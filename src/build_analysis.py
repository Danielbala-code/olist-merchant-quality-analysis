"""Build an honest merchant delivery-quality analysis from the Olist source CSVs.

The analytical grain is seller-order, not parcel. No synthetic operational events
or labels are created. All exclusions and thresholds are explicit below.
"""

from __future__ import annotations

import argparse
import html
import json
import math
from pathlib import Path

import pandas as pd


MIN_MERCHANT_ORDERS = 30
Z_95 = 1.959963984540054


def wilson_interval(successes: pd.Series, totals: pd.Series) -> tuple[pd.Series, pd.Series]:
    proportion = successes / totals
    denominator = 1 + Z_95**2 / totals
    centre = (proportion + Z_95**2 / (2 * totals)) / denominator
    margin = (
        Z_95
        * ((proportion * (1 - proportion) / totals + Z_95**2 / (4 * totals**2)) ** 0.5)
        / denominator
    )
    return centre - margin, centre + margin


def load_sources(data_dir: Path) -> dict[str, pd.DataFrame]:
    required = {
        "orders": "olist_orders_dataset.csv",
        "items": "olist_order_items_dataset.csv",
        "sellers": "olist_sellers_dataset.csv",
        "customers": "olist_customers_dataset.csv",
        "products": "olist_products_dataset.csv",
        "reviews": "olist_order_reviews_dataset.csv",
        "translation": "product_category_name_translation.csv",
    }
    missing = [filename for filename in required.values() if not (data_dir / filename).exists()]
    if missing:
        raise FileNotFoundError(f"Missing required source files: {missing}")

    sources = {name: pd.read_csv(data_dir / filename, low_memory=False) for name, filename in required.items()}
    date_columns = [
        "order_purchase_timestamp",
        "order_approved_at",
        "order_delivered_carrier_date",
        "order_delivered_customer_date",
        "order_estimated_delivery_date",
    ]
    for column in date_columns:
        sources["orders"][column] = pd.to_datetime(sources["orders"][column], errors="coerce")
    sources["items"]["shipping_limit_date"] = pd.to_datetime(
        sources["items"]["shipping_limit_date"], errors="coerce"
    )
    return sources


def build_seller_order_fact(sources: dict[str, pd.DataFrame]) -> pd.DataFrame:
    orders = sources["orders"]
    items = sources["items"]
    products = sources["products"]
    sellers = sources["sellers"]
    customers = sources["customers"]
    reviews = sources["reviews"]
    translation = sources["translation"]

    products = products.merge(translation, on="product_category_name", how="left", validate="m:1")
    products["product_volume_cm3"] = (
        products["product_length_cm"] * products["product_height_cm"] * products["product_width_cm"]
    )
    enriched_items = items.merge(products, on="product_id", how="left", validate="m:1")
    seller_counts = enriched_items.groupby("order_id")["seller_id"].nunique().rename("seller_count")

    seller_order = (
        enriched_items.groupby(["order_id", "seller_id"], as_index=False)
        .agg(
            item_rows=("order_item_id", "size"),
            distinct_products=("product_id", "nunique"),
            distinct_categories=("product_category_name", "nunique"),
            strictest_shipping_limit_at=("shipping_limit_date", "min"),
            item_value=("price", "sum"),
            freight_value=("freight_value", "sum"),
            total_product_weight_g=("product_weight_g", lambda x: x.sum(min_count=1)),
            total_product_volume_cm3=("product_volume_cm3", lambda x: x.sum(min_count=1)),
        )
        .merge(seller_counts, on="order_id", how="left", validate="m:1")
    )

    review_agg = (
        reviews.groupby("order_id", as_index=False)
        .agg(review_count=("review_id", "size"), review_score_mean=("review_score", "mean"))
    )
    order_customer = orders.merge(customers, on="customer_id", how="left", validate="1:1")
    fact = (
        seller_order.merge(order_customer, on="order_id", how="left", validate="m:1")
        .merge(sellers, on="seller_id", how="left", validate="m:1")
        .merge(review_agg, on="order_id", how="left", validate="m:1")
    )

    fact["multi_seller_order"] = fact["seller_count"].gt(1)
    fact["invalid_approval_sequence"] = (
        fact["order_approved_at"].notna()
        & fact["order_purchase_timestamp"].notna()
        & fact["order_approved_at"].lt(fact["order_purchase_timestamp"])
    )
    fact["invalid_carrier_sequence"] = (
        fact["order_delivered_carrier_date"].notna()
        & fact["order_approved_at"].notna()
        & fact["order_delivered_carrier_date"].lt(fact["order_approved_at"])
    )
    fact["invalid_delivery_sequence"] = (
        fact["order_delivered_customer_date"].notna()
        & fact["order_delivered_carrier_date"].notna()
        & fact["order_delivered_customer_date"].lt(fact["order_delivered_carrier_date"])
    )
    fact["valid_chronology"] = ~fact[
        ["invalid_approval_sequence", "invalid_carrier_sequence", "invalid_delivery_sequence"]
    ].any(axis=1)

    delivered_valid = (
        fact["order_status"].eq("delivered")
        & fact["order_delivered_customer_date"].notna()
        & fact["order_estimated_delivery_date"].notna()
    )
    fact["late_delivery"] = pd.array([pd.NA] * len(fact), dtype="boolean")
    fact.loc[delivered_valid, "late_delivery"] = (
        fact.loc[delivered_valid, "order_delivered_customer_date"].dt.normalize()
        > fact.loc[delivered_valid, "order_estimated_delivery_date"].dt.normalize()
    )

    dispatch_valid = fact["order_delivered_carrier_date"].notna() & fact["strictest_shipping_limit_at"].notna()
    fact["late_dispatch"] = pd.array([pd.NA] * len(fact), dtype="boolean")
    fact.loc[dispatch_valid, "late_dispatch"] = (
        fact.loc[dispatch_valid, "order_delivered_carrier_date"]
        > fact.loc[dispatch_valid, "strictest_shipping_limit_at"]
    )

    seconds_per_day = 86_400
    fact["approval_to_carrier_days"] = (
        fact["order_delivered_carrier_date"] - fact["order_approved_at"]
    ).dt.total_seconds() / seconds_per_day
    fact["carrier_to_delivery_days"] = (
        fact["order_delivered_customer_date"] - fact["order_delivered_carrier_date"]
    ).dt.total_seconds() / seconds_per_day
    fact["purchase_to_delivery_days"] = (
        fact["order_delivered_customer_date"] - fact["order_purchase_timestamp"]
    ).dt.total_seconds() / seconds_per_day
    fact.loc[
        fact["invalid_approval_sequence"] | fact["invalid_carrier_sequence"],
        "approval_to_carrier_days",
    ] = pd.NA
    fact.loc[fact["invalid_delivery_sequence"], "carrier_to_delivery_days"] = pd.NA
    fact["same_state_route"] = fact["seller_state"].eq(fact["customer_state"])
    fact["route_proxy"] = fact["seller_state"].fillna("UNK") + " → " + fact["customer_state"].fillna("UNK")
    fact["purchase_month"] = fact["order_purchase_timestamp"].dt.to_period("M").astype("string")
    return fact


def build_scorecards(fact: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    eligible = fact[
        fact["order_status"].eq("delivered")
        & fact["late_delivery"].notna()
        & ~fact["multi_seller_order"]
    ].copy()
    overall_late_rate = float(eligible["late_delivery"].mean())

    seller = (
        eligible.groupby(["seller_id", "seller_state", "seller_city"], dropna=False, as_index=False)
        .agg(
            delivered_orders=("order_id", "nunique"),
            late_deliveries=("late_delivery", "sum"),
            late_delivery_rate=("late_delivery", "mean"),
            late_dispatch_rate=("late_dispatch", "mean"),
            avg_approval_to_carrier_days=("approval_to_carrier_days", "mean"),
            avg_carrier_to_delivery_days=("carrier_to_delivery_days", "mean"),
            avg_purchase_to_delivery_days=("purchase_to_delivery_days", "mean"),
            avg_review_score=("review_score_mean", "mean"),
            item_value=("item_value", "sum"),
            freight_value=("freight_value", "sum"),
        )
    )
    lower, upper = wilson_interval(seller["late_deliveries"], seller["delivered_orders"])
    seller["late_rate_ci95_lower"] = lower
    seller["late_rate_ci95_upper"] = upper
    seller["minimum_volume_met"] = seller["delivered_orders"].ge(MIN_MERCHANT_ORDERS)
    seller["elevated_late_rate"] = seller["minimum_volume_met"] & seller["late_rate_ci95_lower"].gt(
        overall_late_rate
    )
    seller["overall_comparison_rate"] = overall_late_rate
    seller = seller.sort_values(["elevated_late_rate", "late_delivery_rate", "delivered_orders"], ascending=[False, False, False])

    routes = (
        eligible.groupby(["seller_state", "customer_state", "route_proxy"], dropna=False, as_index=False)
        .agg(
            delivered_orders=("order_id", "nunique"),
            late_deliveries=("late_delivery", "sum"),
            late_delivery_rate=("late_delivery", "mean"),
            late_dispatch_rate=("late_dispatch", "mean"),
            avg_carrier_to_delivery_days=("carrier_to_delivery_days", "mean"),
            avg_review_score=("review_score_mean", "mean"),
        )
    )
    routes["on_time_delivery_rate"] = 1 - routes["late_delivery_rate"]
    route_lower, route_upper = wilson_interval(routes["late_deliveries"], routes["delivered_orders"])
    routes["late_rate_ci95_lower"] = route_lower
    routes["late_rate_ci95_upper"] = route_upper
    routes["late_order_share"] = routes["late_deliveries"] / routes["late_deliveries"].sum()
    routes["expected_late_deliveries_at_overall_rate"] = routes["delivered_orders"] * overall_late_rate
    routes["excess_late_deliveries_vs_overall"] = (
        routes["late_deliveries"] - routes["expected_late_deliveries_at_overall_rate"]
    ).clip(lower=0)
    routes = routes[routes["delivered_orders"].ge(MIN_MERCHANT_ORDERS)].sort_values(
        ["late_delivery_rate", "delivered_orders"], ascending=[False, False]
    )

    monthly = (
        eligible.groupby("purchase_month", as_index=False)
        .agg(
            delivered_orders=("order_id", "nunique"),
            late_delivery_rate=("late_delivery", "mean"),
            late_dispatch_rate=("late_dispatch", "mean"),
            avg_approval_to_carrier_days=("approval_to_carrier_days", "mean"),
            avg_carrier_to_delivery_days=("carrier_to_delivery_days", "mean"),
            avg_purchase_to_delivery_days=("purchase_to_delivery_days", "mean"),
            avg_review_score=("review_score_mean", "mean"),
            interstate_share=("same_state_route", lambda values: (~values).mean()),
        )
        .sort_values("purchase_month")
    )
    monthly["on_time_delivery_rate"] = 1 - monthly["late_delivery_rate"]
    return seller, routes, monthly


def build_lane_volume_summary(routes: pd.DataFrame) -> pd.DataFrame:
    """Test whether published low-volume lanes systematically have worse outcomes."""
    labels = ["30–49", "50–99", "100–499", "500–999", "1,000+"]
    working = routes.copy()
    working["volume_band"] = pd.cut(
        working["delivered_orders"],
        bins=[30, 50, 100, 500, 1000, math.inf],
        labels=labels,
        right=False,
    )
    summary = (
        working.groupby("volume_band", observed=True, as_index=False)
        .agg(
            lanes=("route_proxy", "size"),
            delivered_orders=("delivered_orders", "sum"),
            late_deliveries=("late_deliveries", "sum"),
            unweighted_average_lane_late_rate=("late_delivery_rate", "mean"),
        )
    )
    summary["weighted_late_delivery_rate"] = summary["late_deliveries"] / summary["delivered_orders"]
    return summary[
        [
            "volume_band",
            "lanes",
            "delivered_orders",
            "late_deliveries",
            "weighted_late_delivery_rate",
            "unweighted_average_lane_late_rate",
        ]
    ]


def build_action_plan(
    routes: pd.DataFrame,
    lane_types: pd.DataFrame,
    sellers: pd.DataFrame,
) -> pd.DataFrame:
    """Convert observed patterns into explicit, reversible investigation decisions."""
    high_impact = routes.sort_values(
        ["excess_late_deliveries_vs_overall", "late_delivery_rate"], ascending=False
    ).iloc[0]
    low_volume_high_rate = routes[routes["delivered_orders"].lt(100)].sort_values(
        ["late_delivery_rate", "delivered_orders"], ascending=[False, False]
    ).iloc[0]
    overall_late_rate = float(sellers["overall_comparison_rate"].iloc[0])
    healthy_high_volume = routes[routes["late_delivery_rate"].lt(overall_late_rate)].sort_values(
        "delivered_orders", ascending=False
    ).iloc[0]
    between = lane_types[lane_types["lane_type"].eq("Between states")].iloc[0]
    within = lane_types[lane_types["lane_type"].eq("Within state")].iloc[0]
    sp_routes = routes[routes["seller_state"].eq("SP")]
    published_orders = int(routes["delivered_orders"].sum())
    sp_orders = int(sp_routes["delivered_orders"].sum())
    elevated = sellers[sellers["elevated_late_rate"]]

    rows = [
        {
            "finding": "Largest excess-late signal",
            "evidence": (
                f"{high_impact['route_proxy']}: {int(high_impact['delivered_orders']):,} orders, "
                f"{int(high_impact['late_deliveries']):,} late, {high_impact['late_delivery_rate']:.1%} late, "
                f"about {high_impact['excess_late_deliveries_vs_overall']:.0f} above the overall-rate expectation"
            ),
            "decision": "Review lane and carrier-stage evidence first; do not infer a carrier cause.",
            "owner": "Network Operations + KAM",
            "success_measure": "Late orders, on-time rate and carrier-stage days",
            "evidence_limit": "No carrier, hub-scan or exception code",
        },
        {
            "finding": "SP order concentration",
            "evidence": f"SP originates {sp_orders:,} of {published_orders:,} orders ({sp_orders / published_orders:.1%}) in published lanes",
            "decision": "Separate SP → SP from SP → other-state performance instead of treating SP as one market.",
            "owner": "Merchant Quality Analyst",
            "success_measure": "Same-state and outbound lane KPIs reported separately",
            "evidence_limit": "Published lane table excludes lanes below 30 orders",
        },
        {
            "finding": "Healthy high-volume benchmark",
            "evidence": (
                f"{healthy_high_volume['route_proxy']}: {int(healthy_high_volume['delivered_orders']):,} orders, "
                f"{healthy_high_volume['on_time_delivery_rate']:.1%} on time, "
                f"{healthy_high_volume['avg_carrier_to_delivery_days']:.1f} carrier days"
            ),
            "decision": "Use this lane as a benchmark and investigate which observable practices transfer.",
            "owner": "Operations",
            "success_measure": "Stable on-time rate at comparable volume",
            "evidence_limit": "A same-state benchmark may not transfer to longer lanes",
        },
        {
            "finding": "High rate, limited evidence",
            "evidence": (
                f"{low_volume_high_rate['route_proxy']}: {int(low_volume_high_rate['delivered_orders']):,} orders, "
                f"{int(low_volume_high_rate['late_deliveries']):,} late, {low_volume_high_rate['late_delivery_rate']:.1%} late"
            ),
            "decision": "Monitor and gather more observations before treating the percentage as the largest operational priority.",
            "owner": "Merchant Quality Analyst",
            "success_measure": "Late-order count and confidence interval after more volume",
            "evidence_limit": "One additional late order materially changes the rate",
        },
        {
            "finding": "Between-state delivery gap",
            "evidence": (
                f"Between-state: {between['late_delivery_rate']:.1%} late and {between['avg_carrier_to_delivery_days']:.1f} carrier days; "
                f"within-state: {within['late_delivery_rate']:.1%} and {within['avg_carrier_to_delivery_days']:.1f} days"
            ),
            "decision": "Prioritise downstream lane evidence because dispatch rates are similar across the two groups.",
            "owner": "Network Operations",
            "success_measure": "Carrier-stage days and on-time gap by lane",
            "evidence_limit": "State crossing is not a distance or causal measure",
        },
        {
            "finding": "Merchant exception queue",
            "evidence": f"{len(elevated):,} merchants have a 95% Wilson lower bound above the overall late rate",
            "decision": "Split cases between seller-handover review and downstream lane review using the observable stage metrics.",
            "owner": "KAM + Operations",
            "success_measure": "Late dispatch, carrier-stage days and late orders",
            "evidence_limit": "No exception reason or carrier identity",
        },
    ]
    return pd.DataFrame(rows)


def build_diagnostic_slices(fact: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    eligible = fact[
        fact["order_status"].eq("delivered")
        & fact["late_delivery"].notna()
        & ~fact["multi_seller_order"]
    ].copy()
    eligible["delivery_outcome"] = eligible["late_delivery"].map({False: "On time", True: "Late"})
    outcome = (
        eligible.groupby("delivery_outcome", as_index=False)
        .agg(
            delivered_orders=("order_id", "nunique"),
            late_dispatch_rate=("late_dispatch", "mean"),
            avg_approval_to_carrier_days=("approval_to_carrier_days", "mean"),
            avg_carrier_to_delivery_days=("carrier_to_delivery_days", "mean"),
            avg_purchase_to_delivery_days=("purchase_to_delivery_days", "mean"),
            avg_review_score=("review_score_mean", "mean"),
        )
    )
    eligible["lane_type"] = eligible["same_state_route"].map({True: "Within state", False: "Between states"})
    lane = (
        eligible.groupby("lane_type", as_index=False)
        .agg(
            delivered_orders=("order_id", "nunique"),
            late_delivery_rate=("late_delivery", "mean"),
            late_dispatch_rate=("late_dispatch", "mean"),
            avg_carrier_to_delivery_days=("carrier_to_delivery_days", "mean"),
            avg_review_score=("review_score_mean", "mean"),
        )
    )
    return outcome, lane


def build_overall_kpis(fact: pd.DataFrame) -> pd.DataFrame:
    eligible = fact[
        fact["order_status"].eq("delivered")
        & fact["late_delivery"].notna()
        & ~fact["multi_seller_order"]
    ].copy()
    eligible["purchase_month"] = eligible["order_purchase_timestamp"].dt.to_period("M").astype(str)
    monthly_volume = eligible.groupby("purchase_month")["order_id"].nunique()
    typical_monthly_volume = monthly_volume[monthly_volume.ge(100)].mean()
    rows = [
        ("Delivered orders", float(typical_monthly_volume), "Context"),
        ("On-time delivery rate", float((~eligible["late_delivery"].astype(bool)).mean()), "Higher"),
        ("Late dispatch rate", float(eligible["late_dispatch"].mean()), "Lower"),
        ("Approval to carrier days", float(eligible["approval_to_carrier_days"].mean()), "Lower"),
        ("Carrier to delivery days", float(eligible["carrier_to_delivery_days"].mean()), "Lower"),
        ("Purchase to delivery days", float(eligible["purchase_to_delivery_days"].mean()), "Lower"),
        ("Average review score", float(eligible["review_score_mean"].mean()), "Higher"),
        ("Between-state share", float((~eligible["same_state_route"]).mean()), "Context"),
    ]
    return pd.DataFrame(rows, columns=["metric", "value", "direction"])


def build_quality_report(sources: dict[str, pd.DataFrame], fact: pd.DataFrame) -> pd.DataFrame:
    orders = sources["orders"]
    items = sources["items"]
    checks = [
        ("raw_orders", len(orders), "Source order rows"),
        ("seller_order_rows", len(fact), "Analytical seller-order fulfilment rows"),
        ("orders_without_items", (~orders["order_id"].isin(items["order_id"])).sum(), "Orders not represented in item data"),
        ("multi_seller_orders", items.groupby("order_id")["seller_id"].nunique().gt(1).sum(), "Attribution-ambiguous orders"),
        ("missing_carrier_timestamp", orders["order_delivered_carrier_date"].isna().sum(), "All order statuses"),
        ("missing_delivery_timestamp", orders["order_delivered_customer_date"].isna().sum(), "All order statuses"),
        ("invalid_approval_sequence_rows", fact["invalid_approval_sequence"].sum(), "Seller-order rows"),
        ("invalid_carrier_sequence_rows", fact["invalid_carrier_sequence"].sum(), "Seller-order rows"),
        ("invalid_delivery_sequence_rows", fact["invalid_delivery_sequence"].sum(), "Seller-order rows"),
    ]
    return pd.DataFrame(checks, columns=["check", "value", "definition"])


def svg_text(x: float, y: float, value: object, **attributes: object) -> str:
    options = " ".join(f'{key.replace("_", "-")}="{html.escape(str(val))}"' for key, val in attributes.items())
    return f'<text x="{x:.1f}" y="{y:.1f}" {options}>{html.escape(str(value))}</text>'


def save_figures(monthly: pd.DataFrame, seller: pd.DataFrame, figures_dir: Path) -> None:
    figures_dir.mkdir(parents=True, exist_ok=True)
    usable_months = monthly[monthly["delivered_orders"].ge(100)].copy()
    width, height = 1000, 560
    left, right, top, bottom = 90, 30, 70, 100
    plot_width, plot_height = width - left - right, height - top - bottom
    points = []
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
    svg.append('<rect width="100%" height="100%" fill="#FFFFFF"/>')
    svg.append(svg_text(40, 36, "On-time delivery rate by purchase month", font_size=22, font_family="Arial", font_weight="bold", fill="#172B4D"))
    for rate in range(0, 101, 20):
        y = top + plot_height * (1 - rate / 100)
        svg.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" stroke="#D9E2EC"/>')
        svg.append(svg_text(left - 12, y + 5, f"{rate}%", text_anchor="end", font_size=12, font_family="Arial", fill="#52606D"))
    count = max(len(usable_months) - 1, 1)
    for index, row in usable_months.reset_index(drop=True).iterrows():
        x = left + plot_width * index / count
        y = top + plot_height * (1 - float(row["on_time_delivery_rate"]))
        points.append(f"{x:.1f},{y:.1f}")
        if index % 2 == 0:
            svg.append(svg_text(x, top + plot_height + 28, row["purchase_month"], text_anchor="middle", font_size=11, font_family="Arial", fill="#52606D", transform=f"rotate(45 {x:.1f} {top + plot_height + 28:.1f})"))
    svg.append(f'<polyline points="{" ".join(points)}" fill="none" stroke="#1F77B4" stroke-width="3"/>')
    for point in points:
        x, y = point.split(",")
        svg.append(f'<circle cx="{x}" cy="{y}" r="4" fill="#1F77B4"/>')
    svg.append(svg_text(22, top + plot_height / 2, "On-time delivery", text_anchor="middle", font_size=13, font_family="Arial", fill="#334E68", transform=f"rotate(-90 22 {top + plot_height / 2:.1f})"))
    svg.append('</svg>')
    (figures_dir / "monthly_on_time_delivery.svg").write_text("\n".join(svg), encoding="utf-8")

    chart_data = seller[seller["minimum_volume_met"]].copy()
    width, height = 950, 600
    left, right, top, bottom = 90, 40, 80, 75
    plot_width, plot_height = width - left - right, height - top - bottom
    x_min = math.log10(float(chart_data["delivered_orders"].min()))
    x_max = math.log10(float(chart_data["delivered_orders"].max()))
    svg = [f'<svg xmlns="http://www.w3.org/2000/svg" width="{width}" height="{height}" viewBox="0 0 {width} {height}">']
    svg.append('<rect width="100%" height="100%" fill="#FFFFFF"/>')
    svg.append(svg_text(40, 36, f"Merchant volume and late-delivery rate (minimum {MIN_MERCHANT_ORDERS} orders)", font_size=21, font_family="Arial", font_weight="bold", fill="#172B4D"))
    for rate in range(0, 101, 20):
        y = top + plot_height * (1 - rate / 100)
        svg.append(f'<line x1="{left}" y1="{y:.1f}" x2="{left + plot_width}" y2="{y:.1f}" stroke="#E4E7EB"/>')
        svg.append(svg_text(left - 12, y + 5, f"{rate}%", text_anchor="end", font_size=12, font_family="Arial", fill="#52606D"))
    overall = float(chart_data["overall_comparison_rate"].iloc[0])
    overall_y = top + plot_height * (1 - overall)
    svg.append(f'<line x1="{left}" y1="{overall_y:.1f}" x2="{left + plot_width}" y2="{overall_y:.1f}" stroke="#333333" stroke-width="2" stroke-dasharray="8 6"/>')
    svg.append(svg_text(left + plot_width - 5, overall_y - 8, f"Overall {overall:.1%}", text_anchor="end", font_size=12, font_family="Arial", fill="#333333"))
    for _, row in chart_data.iterrows():
        x = left + plot_width * (math.log10(float(row["delivered_orders"])) - x_min) / (x_max - x_min)
        y = top + plot_height * (1 - float(row["late_delivery_rate"]))
        colour = "#C53D43" if bool(row["elevated_late_rate"]) else "#3274A1"
        svg.append(f'<circle cx="{x:.1f}" cy="{y:.1f}" r="4" fill="{colour}" fill-opacity="0.65"/>')
    for volume in [30, 100, 300, 1000]:
        if volume <= chart_data["delivered_orders"].max():
            x = left + plot_width * (math.log10(volume) - x_min) / (x_max - x_min)
            svg.append(svg_text(x, top + plot_height + 28, volume, text_anchor="middle", font_size=12, font_family="Arial", fill="#52606D"))
    svg.append(svg_text(left + plot_width / 2, height - 18, "Delivered orders (log scale)", text_anchor="middle", font_size=13, font_family="Arial", fill="#334E68"))
    svg.append(svg_text(22, top + plot_height / 2, "Late delivery rate", text_anchor="middle", font_size=13, font_family="Arial", fill="#334E68", transform=f"rotate(-90 22 {top + plot_height / 2:.1f})"))
    svg.append('</svg>')
    (figures_dir / "merchant_volume_vs_late_rate.svg").write_text("\n".join(svg), encoding="utf-8")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--data-dir", type=Path, default=Path("data/raw"))
    parser.add_argument("--output-dir", type=Path, default=Path("artifacts"))
    args = parser.parse_args()

    sources = load_sources(args.data_dir)
    fact = build_seller_order_fact(sources)
    seller, routes, monthly = build_scorecards(fact)
    outcome, lane = build_diagnostic_slices(fact)
    lane_volume = build_lane_volume_summary(routes)
    actions = build_action_plan(routes, lane, seller)
    overall = build_overall_kpis(fact)
    quality = build_quality_report(sources, fact)

    args.output_dir.mkdir(parents=True, exist_ok=True)
    fact.to_csv(args.output_dir / "seller_order_fact.csv", index=False)
    seller.to_csv(args.output_dir / "seller_scorecard.csv", index=False)
    routes.to_csv(args.output_dir / "regional_route_scorecard.csv", index=False)
    lane_volume.to_csv(args.output_dir / "lane_volume_summary.csv", index=False)
    actions.to_csv(args.output_dir / "analyst_action_plan.csv", index=False)
    monthly.to_csv(args.output_dir / "monthly_kpis.csv", index=False)
    outcome.to_csv(args.output_dir / "delivery_outcome_comparison.csv", index=False)
    lane.to_csv(args.output_dir / "lane_type_comparison.csv", index=False)
    overall.to_csv(args.output_dir / "overall_kpis.csv", index=False)
    quality.to_csv(args.output_dir / "data_quality_summary.csv", index=False)
    save_figures(monthly, seller, args.output_dir / "figures")

    eligible = fact[
        fact["order_status"].eq("delivered")
        & fact["late_delivery"].notna()
        & ~fact["multi_seller_order"]
    ]
    summary = {
        "analytical_grain": "one row per order_id and seller_id; not a physical parcel",
        "eligible_single_seller_delivered_orders": int(eligible["order_id"].nunique()),
        "late_delivery_rate": round(float(eligible["late_delivery"].mean()), 6),
        "merchants_with_minimum_volume": int(seller["minimum_volume_met"].sum()),
        "merchants_with_statistically_elevated_late_rate": int(seller["elevated_late_rate"].sum()),
        "minimum_merchant_orders": MIN_MERCHANT_ORDERS,
    }
    (args.output_dir / "summary.json").write_text(json.dumps(summary, indent=2), encoding="utf-8")
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
