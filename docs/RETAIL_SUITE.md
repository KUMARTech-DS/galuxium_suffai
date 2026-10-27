# Retail suite: Inventory, Warehouse and Store POS

The Command Center runs a clothing brand's stock and store billing next to Geo, Ads Lab and Campaigns: the same login, the same tab shell, the same Cloud SQL database. Deploy steps: [GCP_REDEPLOY_E2E.md → Release: retail suite](GCP_REDEPLOY_E2E.md#release-retail-suite-inventory-warehouse-store-pos).

## Tabs

| Tab | Page | What it does |
|-----|------|--------------|
| Inventory | `web/inventory.html` (`/inventory`) | Overview (units, stock value at cost and MRP, low stock, sales), catalog of styles and colour / size SKUs with barcodes **and one photo per colour** (for Ads Lab ranking), stock by location and bin, stock moves, incoming POs and transfers, CSV import, price tags, damage and count adjustments |
| Warehouse | `web/warehouse.html` (`/warehouse`) | **Stock Pilot** (see below), purchase orders and goods receipts (part deliveries, shortages), transfers (dispatch, receive), replenishment to par levels, new-store opening plans, locations and bins, suppliers, staff logins (Team), billing settings |
| Store POS | `web/pos.html` (`/pos`) | Scan or search SKUs, GST bill, cash / card / UPI payments (recorded only), printed invoice, returns with credit notes, day close by payment mode and cashier |

All three share `web/retail.js` and `web/retail.css`. The API is `command_center/retail_api.py` under `/v1/retail/*`. Press ⌘K (Ctrl+K) on any retail tab to jump to a section, run a command or ask AURA.

## AURA Stock Pilot

Every time it is opened, the Stock Pilot reads the brand's stock, open transfers and POs, the last 28 days of POS sales and what shoppers around each store already wear (Geo garment demand per PIN), and ranks the next stock moves:

| Kind | When |
|------|------|
| Restock | A store SKU has under 10 days of cover at its recent sales pace (or is out and still selling); a warehouse tops it up to 21 days. Unstocked SKUs that closely match local demand get 2 seed units |
| Rebalance | Another store holds more than 45 days of cover of what this store is short of |
| Reorder | Brand-wide cover (every location, transfers on the road, open POs) is under 21 days and no location can cover it; orders up to 45 days into the warehouse, from the SKU's last supplier |
| Stuck documents | A transfer on the road 3+ days, a draft transfer waiting 2+ days, a PO past its expected date |
| Slow / damaged | 4+ units unsold for 28 days at a store that has had them 21+ days; units in a damaged bin |

Each suggestion shows why (sales per day, on hand, days of cover), units, value and a severity. **Review & create** opens the lines with editable quantities; confirming creates a **draft** transfer (source `aura`) or a **draft** PO. Nothing is dispatched, ordered or received without a person doing it on the Warehouse or Inventory tab. If stock changed since the plan was made, the draft is refused and the plan reloads. **Wardrobe fit by store** scores each store's stock mix (category, colour, size) against local demand and lists the gaps.

- Where: **Warehouse → Stock Pilot** (full view) and the top three suggestions on **Inventory → Overview**. **Store POS** marks the last 2 units of a SKU in the store.
- Who: brand and admin see every location. Staff see suggestions involving their own location; a warehouse login can create transfers out of its warehouse; POs and store-to-store moves stay with the brand.
- AURA (brand and admin): **Ask AURA why** on a card, **Ask AURA** in the header, or chat ("What should I restock today?"). Tools: `get_stock_actions`, `apply_stock_action` and `draft_stock_transfer` (both need the brand to click Confirm), `find_stock`, `get_assortment_fit`, `open_stock_view`, `plan_opening_stock`. The no-model events `stock_brief` and `stock_action` answer without Vertex. AURA never shows internal IDs or tool names.
- Code: `command_center/retail_pilot.py` (rules and thresholds at the top of the file), `web/pilot.js`. API: `GET /v1/retail/pilot`, `POST /v1/retail/pilot/actions/{id}/apply` with `{lines: [{variant_id, qty}], supplier_id?}`.
- Numbers are *Actual (POS)*. Without Geo metrics the Pilot still plans from stock and sales, without seed units or wardrobe-fit scores.

## Roles

| Role | Tabs | Can |
|------|------|-----|
| `brand` (brand HQ) | all, plus the three new ones | Everything for its own brand |
| `admin` | all | Same, on a brand it picks |
| `warehouse_staff` | Warehouse, Inventory | Receive POs, dispatch transfers, replenish, adjust stock, at its own warehouse only |
| `store_staff` | Store POS, Inventory | Sell, return, close the day, receive transfers, adjust stock, at its own store only |

Staff logins live in BigQuery `login_cred_db` with `location_code` (warehouse or store code). The brand creates, disables and resets them on **Warehouse → Team**; a new password is shown once and only its sha256 is stored. Every read and write is scoped to the session's brand, and staff to their location (`retail_scope.py`).

## Money, GST and numbers

- Amounts are stored in integer paise; the UI shows rupees.
- Apparel GST: 5% when the taxable value per piece is at most ₹2,500, otherwise 18% (`RETAIL_GST_*` variables change the threshold and rates). A style can instead carry a fixed rate.
- Prices are tax-inclusive. Each bill line's taxable value plus tax equals what the shopper pays for it; CGST and SGST are equal halves within the state, IGST across states (place of supply from the buyer's GSTIN when given).
- Credit notes reverse the original line in proportion; the last return of a line takes the remainder, so all credits add up to the original exactly.
- Document numbers are gap-free per brand and financial year: `PO2627-00001`, `GRN2627-00001`, `TR2627-00001`, store bills `BLR012627-000001`. A repeated POS request with the same `client_ref` returns the first bill instead of billing twice.
- Stock is a ledger (`retail_stock_moves`) with on-hand per location, bin and SKU (`retail_stock`). Damaged units sit in a separate bin and are not sellable.

## Catalog photos and Ads Lab ranking

The ranking unit is **one style × colour** (sizes are listed only; they never enter the rank score).

1. **Inventory → Catalog → open a style:** each colour gets one product photo (Add / Replace / Remove). Stored at `catalog/{brand}/{product_id}/{uuid}.jpg` (never treated as a customer photo).
2. **Ads Lab → step 2:** tab **From your catalog** (default when the brand has styles) lists style × colour cards with photo, sizes, stock. Pick up to **15**, then **Rank n SKUs**. Tab **Upload photos** keeps today's image-upload flow as a fallback.
3. Catalog jobs skip Gemini re-extraction: attributes come from the catalog (category, colour, material, pattern; size blank or `S/M/L` for 3P export). Compare-pins still uses the catalog photo.
4. API: `GET /v1/ads-lab/catalog-skus`, `POST /v1/ads-lab/campaign-jobs/catalog` with `{items:[{style_code,color}], …audience}`, `POST/DELETE /v1/retail/products/{id}/images`. Migration `013_catalog_images.sql` adds `retail_product_images` and `brand_skus.source` / `retail_product_id` / `style_code`.
5. AURA: `list_catalog_skus` and `rank_catalog_skus` (prefer these when the brand has a catalog).

## Brand SKU match on customer photos

When **suffai-extraction** finishes a customer garment photo, it tries to map the garment to the brand's warehouse catalog (Cloud SQL). Matching needs `DATABASE_URL` on the extraction service (same instance as Command Center).

| Rule | Behaviour |
|------|-----------|
| Brand | Brand Gemini read from the label; only if that brand has active catalog products |
| Exact | Tag SKU / barcode → that variant (size from catalog); style code → product, colour must match (or only one colour) |
| Strict attributes | Category + colour + material + pattern all agree and exactly one style × colour; photo size kept |
| Overwrite | `suffai_ops.commodities` name, colour, brand, material, pattern, gender from the catalog; sleeve/collar stay from the photo. Columns `brand_sku`, `brand_style_code`, `catalog_match` record the hit. Admin shows a **Brand SKU** badge |

No match (or no catalog / no DB) keeps today's photo metadata. Deploy: Cloud SQL 013, BigQuery `migrate_bq_catalog_match.sh`, redeploy extraction then Command Center.

## Links to Geo, Campaigns and AURA

- **Geo → Plan Opening Stock.** Builds an opening assortment for a PIN from the area's garment demand (category, colour and size mix of the selected audience) capped by warehouse stock, then one click creates the transfer.
- **Campaigns → Store Stock & Sales (Actual (POS)).** For the opened campaign: units of its SKUs at stores in and near its PINs, warehouse units, and POS sales in the campaign window against the same number of days before. Catalog-sourced campaign SKUs match by `retail_product_id` + colour; uploaded SKUs still match by garment type and colour. Bills can be tagged with a campaign at the POS.
- **AURA.** `get_retail_snapshot` (brand and admin, read-only) returns aggregate stock, sales and open orders, and the Stock Pilot tools above explain and draft stock moves. They never return customer details or internal IDs.

Simulated campaign metrics stay labelled *Simulated*; numbers from the retail tabs are labelled *Actual (POS)*.

## Data

Cloud SQL migrations `db/migrations/012_retail_ops.sql` (retail ops tables) and `013_catalog_images.sql` (`retail_product_images`, catalog columns on `brand_skus`). The same files build the SQLite schema the tests use (013's `brand_skus` ALTER lines are Cloud SQL only). BigQuery: `migrate_bq_retail.sql` adds `login_cred_db.location_code`; `migrate_bq_catalog_match.sql` adds `commodities.brand_sku`, `brand_style_code`, `catalog_match`.

Shopper name, phone and GSTIN on a bill stay in Cloud SQL for the invoice. Summaries, the snapshot, the Campaigns card and AURA use totals only.

## First day for a brand

1. **Warehouse → Settings:** legal name, GSTIN, address, return days, invoice footer.
2. **Warehouse → Locations:** warehouses and stores (code, state, PIN, GSTIN if the store has its own), then bins in the warehouse.
3. **Inventory → Catalog:** add styles (colours × sizes, MRP, HSN) or import a CSV; add a photo per colour before ranking in Ads Lab.
4. **Warehouse → Suppliers**, then **Purchase orders:** order, receive into bins. Or **Inventory → Adjust & count → Opening stock import** for stock already on hand.
5. **Warehouse → Transfers** (or **Replenishment** once par levels are set): send stock to stores.
6. **Warehouse → Team:** a login for each warehouse operator and cashier.

## Known limits

- Payments are recorded, not processed: no card terminal or UPI gateway integration.
- No GST e-invoicing (IRN / QR from the IRP) or e-way bills. Transfers between your own locations are stock movements, not tax invoices; inter-state transfers between different GSTINs need a tax document outside the suite.
- The POS needs a connection; there is no offline mode.
- Sessions are kept in memory on each Command Center instance. Disabling a login or resetting its password signs it out on the instance that handled the request; with several instances, other instances keep that session until it expires (12 hours, `AUTH_SESSION_TTL_SEC`).
