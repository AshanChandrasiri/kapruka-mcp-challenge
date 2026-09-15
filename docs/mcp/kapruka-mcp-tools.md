# Kapruka MCP — Tool Contracts

Endpoint: `https://mcp.kapruka.com/mcp` (Streamable HTTP, no auth)
Rate limits: 60 requests/min per IP across all tools; 30 `kapruka_create_order`
calls/hour per IP. Server-side cache up to 30 min for reads; writes are never
cached. Full docs: https://mcp.kapruka.com

## kapruka_search_products
Search the catalog by keyword with filters.
- Params: `q`, `category`, `min_price`, `max_price`, `in_stock_only`, `sort`, `limit`, `cursor`, `currency`
- Pagination capped at 3 pages

## kapruka_get_product
Full details for one product by ID (price, stock, variants, images, shipping, URL).
- Params: `product_id`, `currency`

## kapruka_list_categories
Top-level category names with browse URLs — pass a name back into `category` to filter search.
- Params: `depth`

## kapruka_list_delivery_cities
Search the delivery network by canonical name or vernacular alias.
- Params: `query`, `limit` (up to 50 matches)

## kapruka_check_delivery
Deliverability + flat LKR rate for a city/date/product, with a perishable
warning for cake/flower/combo product codes.
- Params: `city`, `delivery_date`, `product_id`

## kapruka_create_order
Creates a guest-checkout order, returns a click-to-pay URL. **Real order,
real money** — no sandbox. Prices locked 60 minutes, multi-currency.
- Params: `cart`, `recipient`, `delivery`, `sender`, `gift_message`, `currency`
- Never call without an explicit prior human confirmation in the graph.

## kapruka_track_order
Status, recipient, items, and timestamped delivery progress for an order.
- Params: `order_number`

## kapruka_render_options_card
Renders 1-4 products as one shareable JPEG "menu" card and returns its URL.
- Not in the original tool count for this project — confirmed live on the
  server as of 2026-09-11.

## Not available on this MCP
No return/refund/cancel-order tool exists. Any "return item" intent is a
fallback response, not something this server can execute.
