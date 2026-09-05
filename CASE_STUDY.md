# Merchant Delivery Quality: decision brief

## Business question

Which merchants and seller-state-to-customer-state lanes should an operations team review first, and does the observable delay accumulate before or after carrier handover?

This is an operational quality review, not a prediction or route-optimisation exercise. The Olist data has no physical parcel ID, carrier, hub scan, locker event, delivery attempt or exception code. The analytical grain is therefore one `order_id`–`seller_id` fulfilment record, explicitly described as a proxy.

## Executive findings

The 95,195 eligible single-seller delivered orders achieved **93.15% on-time delivery**, leaving **6,521 late orders**.

Late orders differ materially across both observable stages:

| Outcome | Orders | Late dispatch | Approval → carrier | Carrier → delivery | Review score |
|---|---:|---:|---:|---:|---:|
| Late | 6,521 | 27.8% | 5.58 days | 27.90 days | 2.27 |
| On time | 88,674 | 7.7% | 2.66 days | 8.00 days | 4.31 |

This supports two separate investigation queues: merchant cut-off/handover issues and downstream delivery issues. It does **not** identify an unobserved root cause.

Of 615 merchants with at least 30 eligible deliveries, **79 have a 95% Wilson confidence interval entirely above the 6.85% overall late-delivery rate**. These merchants are 12.8% of the comparable merchant population, account for 17.6% of eligible orders, and contribute 30.7% of late orders. Their combined late-delivery rate is 12.0%.

Between-state orders show an **8.14% late-delivery rate**, compared with **4.56% within-state**. Their late-dispatch rates are almost the same (9.05% and 9.19%), while carrier-to-delivery time differs sharply (11.92 versus 4.81 days). This makes lane and handover review a sensible next step, while remaining an association rather than proof of causality.

## Analyst-led investigation

I challenged the initial lane table because ranking only by late-delivery rate can make a small sample look like the largest business problem.

- **SP concentration:** SP originates 67,495 of the 93,031 orders represented in published lanes with at least 30 orders, or 72.6%. That concentration should not be treated as one performance result.
- **Healthy benchmark:** SP → SP has 30,313 orders, 95.3% on-time delivery and 4.81 carrier-to-delivery days.
- **Improvement priority:** SP → RJ has 8,048 orders and 1,147 late deliveries. Its 14.3% late rate produces approximately 596 more late orders than expected if it performed at the 6.85% overall rate.
- **Small-sample caution:** PR → AL has the highest published late rate at 31.4%, but that represents 11 late deliveries among only 35 orders. One additional late order would move the rate by approximately 2.9 percentage points.
- **Volume test:** weighted late rates across the five published lane-volume bands range from 6.6% to 8.1% and do not decline consistently as volume increases. Lower volume alone therefore does not explain poorer performance.

The resulting priority method uses three pieces of evidence together: the late rate identifies abnormal performance, the late-order count measures customer impact, and the confidence or volume indicates how stable the percentage is.

## Latest-period readout

For August 2018, the selectable Excel review shows:

- 6,239 eligible deliveries;
- 93.7% on-time delivery, down 2.9 percentage points from July;
- 9.2% late dispatch, broadly flat month on month;
- 2.2 approval-to-carrier days and 5.2 carrier-to-delivery days;
- a 4.34 average review score.

The apparent tension—shorter stage averages but lower on-time delivery—is exactly why an analyst should open the exception tables rather than rely on one aggregate KPI.

## Recommended operating actions

1. **Create two merchant queues.** For elevated merchants with high late dispatch, validate order cut-off, seller processing and carrier-handover evidence with the account team. For elevated merchants with low late dispatch but long carrier-stage time, route the case to network operations.
2. **Review the largest excess-late signal.** Start with SP → RJ because it combines a materially high rate with 1,147 late orders. Network Operations and the account team should review downstream evidence, while avoiding an unsupported carrier-cause claim.
3. **Use SP → SP as a benchmark.** Investigate which observable operating practices can transfer, while recognising that a same-state lane is not directly comparable with longer outbound lanes.
4. **Monitor rate-only exceptions.** Keep PR → AL visible, but wait for more evidence before treating 35 orders as the largest operational priority.
5. **Use control limits for escalation.** Keep the 30-order minimum and 95% interval so one or two failures at a tiny merchant do not create a false priority.
6. **Close the data gap.** In a production environment, request parcel ID, carrier, service mode, hub/locker scans, attempt count and exception codes. Those fields would turn stage localisation into defensible root-cause analysis.

## Controls and limitations

- Multi-seller orders are excluded from merchant outcome attribution.
- On-time delivery compares calendar dates, preventing midnight timestamps from misclassifying same-day delivery.
- Non-monotonic intermediate timestamps suppress the affected duration only; a valid promised-versus-actual delivery outcome is retained.
- The oldest and newest low-volume partial months are excluded from trend charts.
- Seller-state to customer-state is a lane proxy, not a physical route.
- Customer reviews are associated with delivery outcome but cannot be treated as caused only by delivery.

## Interview summary

“I built a merchant-quality review from raw e-commerce events using SQL, Python and spreadsheets. I fixed the analytical grain and attribution rules, then separated pre-carrier and carrier-stage time. I challenged the initial rate-only lane ranking because a 31.4% rate across 35 orders is less actionable than a 14.3% rate producing 1,147 late orders. I introduced excess late orders versus the overall benchmark, which identified SP → RJ as the strongest improvement signal, while SP → SP became the healthy high-volume benchmark. I turned the findings into named operational actions with owners, measures and evidence limits.”
