# SuffAI Command Center: project overview

> Brief for building a presentation. Keep slides simple and visual, and use the colour palette at the end.

---

## 1. One-line summary

SuffAI turns the garments customers send for laundry into **wardrobe intelligence**. Brands use it to decide **which products to promote, to whom, and where to open stores**, without ever seeing personal customer data.

---

## 2. The problem and the idea

| | |
|---|---|
| **Problem for brands** | Brands guess what customers in an area actually wear. Ads target broad demographics, store openings rely on generic footfall data, and creative selection is gut feel. |
| **What SuffAI has** | Every laundry order produces garment photos. Together they form a real, continuously updated view of what people in each PIN code own and wear: types, colours, patterns, sizes, materials and brands. |
| **The idea** | Use AI to read those photos, build a private wardrobe per customer, and expose **aggregated, PIN-level insight** to brands through one Command Center. |

---

## 3. Who uses it

| Role | Sees | Purpose |
|---|---|---|
| **Brand** | Geo, Ads Lab, Campaigns | Find areas, rank their products for an audience, run campaigns. Competitor brands are masked. |
| **Customer** | Style Assistant | Personal outfit picks that match their own wardrobe |
| **Admin** (SuffAI team) | Everything, plus the Admin tab | Customer, garment, brand and campaign intelligence across the platform |

An AI assistant, **AURA**, sits on every page and can explain data or run tasks for the user.

---

## 4. End-to-end flow (the big picture)

```
Garment photo (laundry order)  ──┐
                                 ├─► Cloud Storage ─► AI extraction (Gemini) + visual fingerprint (Vertex AI)
Brand SKU images (Ads Lab)  ─────┘                         │
                                                           ▼
                         Cloud SQL (wardrobes, PIN counts, SKUs)  +  BigQuery (analytics)
                                                           │
     ┌───────────────┬────────────────┬────────────────────┼──────────────────┬──────────────┐
     ▼               ▼                ▼                    ▼                  ▼              ▼
   Geo          Ads Lab ranking    Campaigns (1P/3P)   Style Assistant      Admin          AURA
 (where)          (what)            (activate)          (customer)        (oversight)   (assistant)
```

**Simple story for a slide:** *Photo → AI understands the garment → private wardrobe and PIN trends → brands decide where, what and to whom → customers get personal picks.*

---

## 5. Features

### 5.1 Geo Intelligence: *where should the brand be?*
- Map of PIN-code hubs with demand, estimated footfall and the brand's share of local wardrobes against competitors.
- **Open-store decision** per PIN: **Yes / Consider / No**, with a plain-English explanation and market brief.
- **4 km rule:** if the brand already has a store within 4 km, the answer is "Serve via existing store", and the app gives a **stock and assortment routing guide** (which garment types, colours and sizes to stock).
- **External market check** for open areas: footfall, retail rents and competition, from Google Search.
- **Store plan:** stock mix, expected walk-ins and revenue range, validated against SuffAI's own data.

### 5.2 Ads Lab: *which of my products fit this audience?*
1. The brand picks an audience: PIN codes, age, gender and RFM tier (how recently, how often and how much customers spend).
2. Sees the **Audience Taste Profile**: top colours, patterns, sizes, garment types, materials and brands.
3. Uploads SKU images and clicks **Extract & rank**.
4. AI reads each product and compares it with what that audience already wears.
5. Gets a **ranked deck plus a ranking matrix**, with a score from 0 to 100 per product.
6. Next step: run on SuffAI (1P) or export a CSV for other ad platforms (3P).

**Rank score = 55% visual match + 15% local popularity + 30% attribute fit.**

### 5.3 Campaigns (1P): *activate on SuffAI's own channels*
- Five-step setup: Audience → Creatives (single or A/B) → Objective and channels (In-app, WhatsApp, SMS) → Message → Budget and schedule.
- **Recommended budget** and **low / expected / high outcome estimates**.
- Launch, then track: delivery, revenue vs spend, ROAS, conversions by channel, budget pacing, and product fit.
- Performance numbers are **Simulated** in this version. No messages are sent.

### 5.4 Style Assistant: *for customers*
- Personal picks from brand campaigns that **pair with garments the customer already owns** ("Complete the look").
- Computed privately from the customer's own wardrobe.

### 5.5 Admin Intelligence: *for the SuffAI team*
- **Customer Intelligence:** RFM tiers, age and gender spread, customer directory, and per-customer profile with garment attributes and spending momentum.
- **Wardrobe & Garments**, **Brands Intelligence**, **Campaigns Management**.

### 5.6 AURA: *AI assistant on every page*
- Answers questions about the data on screen and runs tasks: set filters, start ranking, open a campaign, get a geo brief.
- Proactive: suggests the next step after an upload and summarises results when ranking finishes.
- Guarded: blocks prompt-injection, strips personal data, masks competitors, hides internal errors.

---

## 6. AI use cases

| # | AI use case | Model / service | What it does | Where it shows up |
|---|---|---|---|---|
| 1 | **Garment attribute extraction** | Gemini 2.0 Flash (Vertex AI) | Reads a photo and labels it: type, colour, pattern, material, size, brand, style, with a confidence per field | Ads Lab, wardrobes, Admin |
| 2 | **Visual fingerprint and similarity** | Vertex AI multimodal embeddings (1,408 numbers per image) | Measures how alike two garments look; removes repeat washes of the same item; matches brand SKUs to local wardrobes | Ads Lab ranking, Style Assistant, PIN counts |
| 3 | **SKU ranking** | Embeddings + attributes + audience profile | Scores each product for a chosen audience (0 to 100) | Ads Lab, Campaigns |
| 4 | **Personal recommendations** | Embeddings + wardrobe taste profile | Picks brand products that complement what a customer owns | Style Assistant |
| 5 | **Store-opening decision** | Gemini 2.5 Flash + server rules | Yes / Consider / No with explanation and brief | Geo |
| 6 | **Store locator and market check** | Gemini with Google Search grounding | Finds a brand's existing stores; checks footfall, rent and competition | Geo |
| 7 | **Store plan estimates** | Gemini 2.5 Flash | External estimates of walk-ins, conversion and bill size, blended with internal data | Geo |
| 8 | **AURA agent** | Gemini with tool calling | Conversational assistant that can act in the UI | All pages |

**Responsible AI:** the AI never infers gender, age or income from images; "unknown" is allowed and brands are never guessed; low-confidence results go to review; no personal data is sent to the AI; brand images never enter customer data.

---

## 7. Business use cases and value

| For | Use case | Value |
|---|---|---|
| **Brands: marketing** | Rank creatives for a precise audience before spending | Better-fitting ads, less wasted spend |
| **Brands: merchandising** | Know which colours, sizes and types sell in each PIN code; stock routing for existing stores | Right stock in the right place, fewer markdowns |
| **Brands: expansion** | Open-store decision with the 4 km cannibalisation rule and external market check | Data-backed store openings, lower capex risk |
| **Brands: activation** | 1P campaigns on SuffAI channels, or CSV export to other platforms | One flow from insight to campaign |
| **Customers** | Personal picks that match their wardrobe | Useful, relevant suggestions; more engagement |
| **SuffAI** | New revenue line: insight and ad platform on top of laundry | Monetises existing data, privacy-first |

**Key differentiator:** insight comes from what people **actually own and wear**, not from clicks or surveys, and it's updated with every laundry order.

---

## 8. Technology (one slide)

| Layer | Google Cloud service |
|---|---|
| App and APIs | **Cloud Run**: Command Center (UI and API), Extraction worker, Similarity service |
| Images | **Cloud Storage** |
| Events and jobs | **Pub/Sub** (new photos), **Cloud Tasks** (background ranking jobs) |
| AI | **Vertex AI**: Gemini 2.0 / 2.5 Flash, multimodal embeddings, Google Search grounding |
| Data | **Cloud SQL** (live data), **BigQuery** (analytics) |

---

## 9. Privacy and trust (one slide)
- Brands see **aggregated, PIN-level** data only; competitor names are masked.
- Admin views mask sensitive fields; no customer personal data goes to the AI.
- AURA blocks prompt-injection and hides internal errors.
- Simulated metrics are clearly labelled **Simulated**.

---

## 10. Suggested slide outline (10–12 slides)
1. Title: *SuffAI Command Center: wardrobe intelligence for fashion brands*
2. The problem brands face
3. The SuffAI idea: laundry photos become wardrobe insight
4. Who uses it (Brand, Customer, Admin, AURA)
5. End-to-end flow diagram
6. Geo Intelligence
7. Ads Lab: upload → AI → ranked products (include the rank formula)
8. Campaigns (1P) and Style Assistant
9. AI use cases (table or icon grid)
10. Business value
11. Technology on Google Cloud
12. Privacy, and what's next

---

## 11. Design guide for the deck (match the UI)

**Colour palette**

| Name | Hex | Use |
|---|---|---|
| Plum (primary) | `#6B3A55` | Titles, primary buttons, key shapes, icons |
| Plum dark | `#552C43` | Hover and emphasis, dark headers |
| Wine | `#2A0F1F` | Dark hero or title backgrounds |
| Wine 2 | `#4A1A33` | Gradients with Wine |
| Ink | `#3B2733` | Body text |
| Muted | `#8A7480` | Secondary text, captions |
| Mauve | `#C59FB0` | Accents, icons on dark backgrounds |
| Cream | `#FAF6EE` | Main slide background |
| Raised | `#F6EEF0` | Cards and panels |
| Line | `#EAD9E0` | Borders and dividers |
| Sage | `#9CB09D` | Soft secondary accent |
| Gold | `#C9A24A` / `#F3D58A` | Highlights and callouts (sparingly) |
| Success / Danger | `#3F7D3B` / `#A3301F` | Yes/No, good/bad indicators |

**Fonts:** headings in **Cormorant Garamond** (elegant serif); body text in **Outfit** (clean sans-serif); numbers in **IBM Plex Mono**.

**Style:** cream background, white or `#F6EEF0` rounded cards with `#EAD9E0` borders, plum headings, generous white space, simple line icons. Use a dark wine-to-plum gradient for the title and section-divider slides. Use gold sparingly for key numbers. Tag any simulated metric with a small **Simulated** label.
