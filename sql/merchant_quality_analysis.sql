-- PostgreSQL-compatible analysis.
-- Grain: one row per order_id + seller_id. This is a fulfilment proxy, not a parcel.

WITH item_enriched AS (
    SELECT
        oi.order_id,
        oi.seller_id,
        oi.order_item_id,
        oi.product_id,
        oi.shipping_limit_date,
        oi.price,
        oi.freight_value,
        p.product_weight_g,
        p.product_length_cm * p.product_height_cm * p.product_width_cm AS product_volume_cm3
    FROM olist_order_items oi
    LEFT JOIN olist_products p USING (product_id)
),
seller_counts AS (
    SELECT order_id, COUNT(DISTINCT seller_id) AS seller_count
    FROM item_enriched
    GROUP BY order_id
),
seller_order AS (
    SELECT
        order_id,
        seller_id,
        COUNT(*) AS item_rows,
        COUNT(DISTINCT product_id) AS distinct_products,
        MIN(shipping_limit_date) AS strictest_shipping_limit_at,
        SUM(price) AS item_value,
        SUM(freight_value) AS freight_value,
        SUM(product_weight_g) AS total_product_weight_g,
        SUM(product_volume_cm3) AS total_product_volume_cm3
    FROM item_enriched
    GROUP BY order_id, seller_id
),
fact AS (
    SELECT
        so.*,
        sc.seller_count,
        (sc.seller_count > 1) AS multi_seller_order,
        o.order_status,
        o.order_purchase_timestamp,
        o.order_approved_at,
        o.order_delivered_carrier_date,
        o.order_delivered_customer_date,
        o.order_estimated_delivery_date,
        c.customer_state,
        s.seller_state,
        CASE
            WHEN o.order_status = 'delivered'
             AND o.order_delivered_customer_date IS NOT NULL
            THEN (DATE(o.order_delivered_customer_date) > DATE(o.order_estimated_delivery_date))::int
        END AS late_delivery,
        CASE
            WHEN o.order_delivered_carrier_date IS NOT NULL
            THEN (o.order_delivered_carrier_date > so.strictest_shipping_limit_at)::int
        END AS late_dispatch
    FROM seller_order so
    JOIN seller_counts sc USING (order_id)
    JOIN olist_orders o USING (order_id)
    JOIN olist_customers c USING (customer_id)
    JOIN olist_sellers s USING (seller_id)
),
eligible AS (
    SELECT *
    FROM fact
    WHERE order_status = 'delivered'
      AND late_delivery IS NOT NULL
      AND NOT multi_seller_order
)
SELECT
    seller_id,
    seller_state,
    COUNT(DISTINCT order_id) AS delivered_orders,
    AVG(late_delivery) AS late_delivery_rate,
    AVG(late_dispatch) AS late_dispatch_rate,
    AVG(CASE WHEN order_delivered_carrier_date >= order_approved_at
        THEN EXTRACT(EPOCH FROM (order_delivered_carrier_date - order_approved_at)) / 86400.0 END)
        AS avg_approval_to_carrier_days,
    AVG(CASE WHEN order_delivered_customer_date >= order_delivered_carrier_date
        THEN EXTRACT(EPOCH FROM (order_delivered_customer_date - order_delivered_carrier_date)) / 86400.0 END)
        AS avg_carrier_to_delivery_days
FROM eligible
GROUP BY seller_id, seller_state
HAVING COUNT(DISTINCT order_id) >= 30
ORDER BY late_delivery_rate DESC, delivered_orders DESC;

-- Rank lanes by customer impact first, while retaining rate and stage evidence.
SELECT
    seller_state || ' → ' || customer_state AS lane_proxy,
    seller_state,
    customer_state,
    COUNT(DISTINCT order_id) AS delivered_orders,
    SUM(late_delivery) AS late_deliveries,
    1.0 - AVG(late_delivery) AS on_time_delivery_rate,
    AVG(late_delivery) AS late_delivery_rate,
    AVG(late_dispatch) AS late_dispatch_rate,
    AVG(CASE WHEN order_delivered_customer_date >= order_delivered_carrier_date
        THEN EXTRACT(EPOCH FROM (order_delivered_customer_date - order_delivered_carrier_date)) / 86400.0 END)
        AS avg_carrier_to_delivery_days
FROM eligible
GROUP BY seller_state, customer_state
HAVING COUNT(DISTINCT order_id) >= 30
ORDER BY late_deliveries DESC, late_delivery_rate DESC;
