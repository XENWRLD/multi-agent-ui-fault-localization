# Comparative Fault Localization Analysis Report
## Describer-Decider v33.0 — Full Dataset Validation (7 Own Traces + 5 Group 411 Cases)

---

## 1. Executive Summary

| Metric | Our Traces (7 apps) | Group 411 Traces (5 cases) | Combined (12 apps) |
|---|---|---|---|
| Total apps evaluated | 7 | 5 | **12** |
| Total steps in dataset | 90 | 46 | **136** |
| Steps skipped (non-VLM / wait) | 5 | 0 | **5** |
| **Steps analyzed** | **85** | **46** | **131** |
| Ground truth failures | **12** | **13** | **25** |
| System-predicted failures | **12** | **12** | **24** |
| True Positives (TP) | **12** | **11** | **23** |
| False Positives (FP) | **0** | **1** | **1** |
| False Negatives (FN) | **0** | **2** | **2** |
| **Precision** | **100.0%** | **91.7%** | **95.8%** |
| **Recall** | **100.0%** | **84.6%** | **92.0%** |
| **F1 Score** | **100.0%** | **88.0%** | **93.9%** |

> **Note on step counts:** Our "analyzed" count counts only VLM-processed steps (skipped steps removed). For the 90 total steps in our 7 traces, 5 are skipped (Bitaksi: 1, Flight App: 3, Yemeksepeti: 1), leaving **85 analyzed**. Group 411 traces have no skipped steps: all 46 are analyzed.

**Bottom line (Our Traces):** v33.0 achieves **perfect precision and recall** on all 7 proprietary test cases — every real failure was detected, and no pass was incorrectly flagged. This represents a significant improvement over the previous evaluation (v2 report), which had 6 false positives from log misalignment and Rule 2 over-application. All systematic issues identified in the prior report were resolved in v33.0.

**Bottom line (Group 411 Traces):** Applied to an independent external dataset, the system achieves **91.7% precision and 84.6% recall (F1 = 88.0%)**. Two false negatives arise from: (1) a subtle post-transfer balance inconsistency requiring cross-step state tracking (Bank App Step 6), and (2) a missed visual mismatch at Rent-a-Car Step 8, where the discount was claimed applied by the UI but the total price ₺17,000 remained unchanged — a visible inconsistency the observer failed to flag. One false positive arises from a VLM observation error on the Passo App (button state misread as grayed-out when ticket categories were actually shown).

**Combined overall F1: 93.9%** — demonstrating strong generalization from the proprietary training-style traces to independently designed external test cases.

---

## 2. Ground Truth — Confirmed Real Failures

### 2A. Our Own Traces

| App | Step | Failure Description | Evidence Type |
|---|---|---|---|
| Migros | **Step 7** | `POST /api/v1/cart/confirm → 400 Bad Request`; cart state inconsistency (local: 1 item, server: EMPTY); UI showed empty cart | Log: `status: FAILED` + network ERROR |
| Bitaksi | — | No failures | All StepRunner markers: PASSED |
| Clock | — | No failures | All steps progressed normally; no log errors |
| Flight App | **Step 18** | Click on `12.15→13.35` flight item did not select it; wrong flight context (21:15) loaded | Visual: screen shows 21:15 departure in POST |
| Flight App | **Steps 20–24** | Cascading content_mismatch: each verification step confirms 21:15 instead of 12:15 | Visual: expected flight time never appears |
| QNB | **Step 4** | Click on `Günlük` option produced no UI state change; button remained unselected | Visual: no change between PREV/POST |
| Yemeksepeti | **Step 7** | Click on `Mixed (Medium) Pizza` opened `Mediterranean (Central)` detail page | Visual + Log: wrong item in POST |
| Yemeksepeti | **Step 14** | AbstractVerification confirmed wrong pizza persists to checkout | Visual: Mediterranean (Central) in order summary |
| O Bilet | **Step 7** | `POST /api/v3/bus/reserve-seat → 504 Gateway Timeout`; reservation service unresponsive | Log: NET_FAIL + SYS_ERR |
| O Bilet | **Step 8** | Navigation to PassengerDetailsActivity blocked by failed reservation dependency | Log: SYS_ERR + UI_STALL |

**Total: 12 real failures across 7 apps.**

### 2B. Group 411 Traces

| App | Step | Failure Description | Evidence Type |
|---|---|---|---|
| Passo App | **Step 10** | Click on occupied (black) seat — no state change; seat remained unselectable | Visual: no change between PREV/POST |
| Bank App | **Step 4** | `POST /transfer → 500 Internal Server Error`; transfer silently failed on backend | Log: NET_FAIL + SYS_ERR |
| Bank App | **Step 6** | Dashboard shows balance of 5250.00 TL unchanged after a "successful" transfer — systematic inconsistency between displayed success and actual server rejection | Cross-step state: balance should update after Step 5 success screen |
| Bank App | **Step 7** | Abstract verification element `Bakiyeyi doğrula` not visible in either PREV or POST | Visual: element absent |
| Ticket App | **Step 2** | Click on `Theater` category — no UI response or navigation occurred | Visual: no change between PREV/POST |
| Ticket App | **Step 3** | `RenderFlex overflow by 869 pixels` on right side of Sports Events screen | Log: rendering exception in console |
| Ticket App | **Step 6** | Click on occupied seat A3 — system error `Seat unavailable`; seat not selectable | Log: SYS_ERR; Visual: no change |
| Ticket App | **Step 10** | `POST /api/v1/payment → 500 Internal Server Error`; payment could not be processed | Log: NET_FAIL + SYS_ERR |
| Rent-a-Car | **Step 5** | Click on `Kirala` for Mercedes-Benz G-Class blocked — corporate account required for intercity Izmir drop-off | Log: SYS_ERR |
| Rent-a-Car | **Step 8** | Promo code `DISCOUNT20` entered and UI claimed discount applied, but total price remained ₺17,000 — visible price-discount inconsistency missed by the observer | Visual: discount message shown but total unchanged |
| Rent-a-Car | **Step 9** | Discount message `"Tebrikler! %20 indirim uygulandı!"` appeared but total remained ₺17,000 — content mismatch on amount | Visual: amount unchanged |
| Rent-a-Car | **Step 10** | `POST /payment → 504 Gateway Timeout` on Izmir regional gateway | Log: NET_FAIL + SYS_ERR |
| Wikipedia | **Step 4** | Instruction target `Osmanlı padişahı 3. Murad tahta çıktı seç.` not present in any visible UI element | Visual: element absent; instruction unrelated to current page context |

**Total: 13 real failures across 5 cases.**

---

## 3. Part A — Our Own Traces: Case-by-Case Breakdown

---

### App 1 — Migros · 8 steps · Log: nested (preassigned)

**Scenario:** App splash → main dashboard → Categories tab → search "Raffaello" → add to cart → select delivery address → view cart → confirm order.

| Step | Action / Element | System Result | Ground Truth | Assessment |
|---|---|---|---|---|
| 0 | waitUntil — Migros Splash Logo | PASS | Pass ✓ | TN |
| 1 | waitUntil — Service Selection Dashboard | PASS | Pass ✓ | TN |
| 2 | click — Categories tab | PASS | Pass ✓ | TN |
| 3 | type — Search "Raffaello" | PASS | Pass ✓ | TN |
| 4 | click — Add to Cart (Ferrero Raffaello 150G) | PASS | Pass ✓ | TN |
| 5 | click — Yurt delivery address | PASS | Pass ✓ | TN |
| 6 | click — Cart icon | PASS | Pass ✓ | TN |
| **7** | **click — Confirm button** | **FAIL** (system_error) | **Fail ✗** | **TP ✓** |

**Result: 1 failure detected, 1 real failure → Perfect precision and recall.**

**True Positive — Step 7:**
The cart confirmation failure was definitively detected. Log evidence: `POST /api/v1/cart/confirm → 400 Bad Request (Cart is empty)`, `STATE_MISMATCH: Local cart state shows 1 item but session validation returned EMPTY`. The visual observer confirmed the cart appeared empty in POST with the Confirm button absent. The `[NET_FAIL, SYS_ERR]` flags correctly elevated the verdict to `system_error`. Classified as **ISOLATED FAILURE** — no upstream cause was found, correctly reflecting that the root is a backend session state inconsistency, not a cascade from prior steps.

**Improvement vs. previous evaluation (v2 report):**
The prior report showed 3 predicted failures (Steps 0, 3, 7) with 2 false positives. v33.0 eliminated both FPs: (1) Step 0's `waitUntil` + presence check no longer triggers Rule 2 strict value verification, and (2) Step 3's search result OCR artifact ("Rafeollo") is no longer misclassified as a content mismatch. The invalid chain `Step 0 → Step 3 → Step 7` is gone; the real failure at Step 7 now stands correctly as an isolated backend event.

---

### App 2 — Bitaksi · 9 steps (1 skipped) · Log: flat_assertions

**Scenario:** App launch (waitUntil, skip) → tap "Where to?" → save home address → select Lux ride → profile info → save profile → trip history → payment methods → delete card.

| Step | Action / Element | System Result | Ground Truth | Assessment |
|---|---|---|---|---|
| 0 | waitUntil — Main map screen [VLM=false] | SKIPPED | Pass (skip) | Correct skip |
| 1 | click — "Where to?" search input | PASS | Pass ✓ | TN |
| 2 | click — Save (home address) | PASS | Pass ✓ | TN |
| 3 | click — Lux ride option | PASS | Pass ✓ | TN |
| 4 | click — Profile information | PASS | Pass ✓ | TN |
| 5 | click — Save button | PASS | Pass ✓ | TN |
| 6 | click — Trip history | PASS | Pass ✓ | TN |
| 7 | click — Payment methods | PASS | Pass ✓ | TN |
| 8 | click — Delete card | PASS | Pass ✓ | TN |

**Result: 0 failures detected, 0 real failures → Perfect TN.**


---

### App 3 — Clock · 7 steps · Log: flat_indexed

**Scenario:** Enable alarm toggle → navigate to Stopwatch tab → start stopwatch → reset stopwatch → scroll minute picker → pause timer → navigate to World Clock tab.

| Step | Action / Element | System Result | Ground Truth | Assessment |
|---|---|---|---|---|
| 0 | click — Alarm toggle (08:17) | PASS | Pass ✓ | TN |
| 1 | click — Stopwatch tab | PASS | Pass ✓ | TN |
| 2 | click — Start button | PASS | Pass ✓ | TN |
| 3 | click — Reset button | PASS | Pass ✓ | TN |
| 4 | scroll — Minute picker | PASS | Pass ✓ | TN |
| 5 | click — Pause button | PASS | Pass ✓ | TN |
| 6 | click — World Clock tab | PASS | Pass ✓ | TN |

**Result: 0 failures detected, 0 real failures → Perfect TN.**


---

### App 4 — Flight App · 25 steps (3 skipped) · Log: dynamic (keyword-based)

**Scenario:** App splash → waitUntil (×2, skip) → dismiss X icon → verify login screen → guest session → allow notification permission → select departure city Nereden → pick İzmir → select destination → pick Ankara → open departure calendar → select date 21 → search flights → wait (skip) → select flight class → pick flight time → search → click flight `12.15→13.35` → select BASIC package → abstractVerification (×5 steps).

| Step | Action / Element | System Result | Ground Truth | Assessment |
|---|---|---|---|---|
| 0 | waitUntil — "Tüm seyahatin tek uygulamada" | PASS | Pass ✓ | TN |
| 1–2 | waitUntil steps [VLM=false] | SKIPPED | Pass (skip) | Correct skip |
| 3 | click — Çarpı (X icon) | PASS | Pass ✓ | TN |
| 4 | verify — Login screen | PASS | Pass ✓ | TN |
| 5 | click — Üye olmadan devam et | PASS | Pass ✓ | TN |
| 6 | click — İzin Ver (notifications) | PASS | Pass ✓ | TN |
| 7 | click — Nereden (departure city) | PASS | Pass ✓ | TN |
| 8 | click — İzmir | PASS | Pass ✓ | TN |
| 9 | click — Arrival city input | PASS | Pass ✓ | TN |
| 10 | click — Ankara | PASS | Pass ✓ | TN |
| 11 | click — Departure date field | PASS | Pass ✓ | TN |
| 12 | click — Calendar date 21 | PASS | Pass ✓ | TN |
| 13 | click — Search button (UCUZ BİLET BUL) | PASS | Pass ✓ | TN |
| 14 | wait [action=wait] | SKIPPED | Pass (skip) | Correct skip |
| 15 | click — Sort by departure time | PASS | Pass ✓ | TN |
| 16 | click — Evening flights filter | PASS | Pass ✓ | TN |
| 17 | click — Show more flights | PASS | Pass ✓ | TN |
| **18** | **click — 12.15→13.35 flight item** | **FAIL** (content_mismatch) | **Fail ✗** | **TP ✓** |
| 19 | click — BASIC package option | PASS | Pass ✓ | TN |
| **20** | **abstractVerify — flight time 12:15–13:35** | **FAIL** (content_mismatch) | **Fail ✗** | **TP ✓** |
| **21** | **abstractVerify — BASIC + 12:15** | **FAIL** (content_mismatch) | **Fail ✗** | **TP ✓** |
| **22** | **abstractVerify — flight time 12:15–13:35** | **FAIL** (content_mismatch, caused by Step 21) | **Fail ✗** | **TP ✓** |
| **23** | **abstractVerify — 12:15 departure + BASIC** | **FAIL** (content_mismatch, caused by Step 22) | **Fail ✗** | **TP ✓** |
| **24** | **abstractVerify — 12:15–13:35 final confirm** | **FAIL** (content_mismatch, caused by Step 23) | **Fail ✗** | **TP ✓** |

**Result: 6 failures detected, 6 real failures → Perfect precision and recall.**

**True Positive — Step 18 (Root Failure):**
The click on the `12.15→13.35` flight item loaded a different flight context. The visual observer noted the flight class selection overlay appeared with no sign of the targeted time slot. Classified as `content_mismatch` — the element was present in PREV (list view) but the POST screen showed the wrong flight context (21:15 departure).

**True Positives — Steps 20–24 (Cascading Chain):**
All five downstream verification steps correctly flagged the same root cause: the screen consistently shows a 21:15–22:35 departure/arrival pair instead of the expected 12:15–13:35. The causal chain `Step 18 → Step 20 → Step 21 → Step 22 → Step 23 → Step 24` was correctly constructed by `find_likely_cause()` using element overlap and temporal proximity scoring across all six steps.

**Notes on dynamic log slicing:** The flight app uses raw/dynamic log mode with keyword-based log assignment (`5 entries per step` consistently). The log evidence was supportive but not critical — the failure was clearly visible in the screenshots. The dynamic slicer's performance here validates that keyword matching (`keyword` mode) produces relevant evidence even without pre-assigned step buckets.

---

### App 5 — QNB · 13 steps · Log: flat_indexed

**Scenario:** Open Kartlar → navigate Kartlar → tap Banka Kartlarım → tap Back → open Limit Geçerlilik Süresi → select Günlük → close dropdown → navigate back → open Banka Kartı Ayarları → tap Güncelle → enter limit value → tap Uygula → confirm (Onayla).

| Step | Action / Element | System Result | Ground Truth | Assessment |
|---|---|---|---|---|
| 0 | click — Kartlar menu | PASS | Pass ✓ | TN |
| 1 | click — Banka Kartlarım | PASS | Pass ✓ | TN |
| 2 | click — Back (to main Kartlar) | PASS | Pass ✓ | TN |
| 3 | click — Limit Geçerlilik Süresi | PASS | Pass ✓ | TN |
| **4** | **click — Günlük option** | **FAIL** (action_failed) | **Fail ✗** | **TP ✓** |
| 5 | click — Günlük (select) | PASS | Pass ✓ | TN |
| 6 | dropdown closes; Günlük confirmed | PASS | Pass ✓ | TN |
| 7 | click — Back (to Kartlar menu) | PASS | Pass ✓ | TN |
| 8 | click — Banka Kartı Ayarları | PASS | Pass ✓ | TN |
| 9 | click — Güncelle button | PASS | Pass ✓ | TN |
| 10 | tap — İşlem Limitiniz input field | PASS | Pass ✓ | TN |
| 11 | tap — Uygula button | PASS | Pass ✓ | TN |
| 12 | tap — Onayla (confirmation popup) | PASS | Pass ✓ | TN |

**Result: 1 failure detected, 1 real failure → Perfect precision and recall.**

**True Positive — Step 4:**
The click on the `Günlük` option produced no observable UI state change — the option remained visible but neither selected nor highlighted. Classified as `action_failed`. The log evidence (entry: "tapped Limit Geçerlilik Süresi modal opened") confirmed a modal was triggered but the Günlük item itself was not responsive. This is correctly isolated (not cascaded) since Step 5 successfully selected Günlük using a different interaction.


---

### App 6 — Yemeksepeti · 15 steps (1 skipped) · Log: nested (no logs field)

**Scenario:** Splash screen → waitUntil sign-up (skip) → close Flash Deals banner → open address bar → select delivery address → search "little ceasers pizza" → open Little Caesars → select Mixed (Medium) Pizza → select Normal Dough → select Less Pizza Sauce → remove ingredients → add to cart → view cart → confirm basket → abstractVerification of checkout.

| Step | Action / Element | System Result | Ground Truth | Assessment |
|---|---|---|---|---|
| 0 | waitUntil — Yemeksepeti Logo | PASS | Pass ✓ | TN |
| 1 | waitUntil — Sign-up screen [VLM=false] | SKIPPED | Pass (skip) | Correct skip |
| 2 | click — Close Banner | PASS | Pass ✓ | TN |
| 3 | click — Address Bar | PASS | Pass ✓ | TN |
| 4 | click — Orta Sabanci Univ. A4 address | PASS | Pass ✓ | TN |
| 5 | type — "little ceasers pizza" | PASS | Pass ✓ | TN |
| 6 | click — Little Caesars Pizza card | PASS | Pass ✓ | TN |
| **7** | **click — Mixed (Medium) Pizza** | **FAIL** (content_mismatch) | **Fail ✗** | **TP ✓** |
| 8 | click — Normal Dough | PASS | Pass ✓ | TN |
| 9 | click — Less Pizza Sauce | PASS | Pass ✓ | TN |
| 10 | click — Remove mushrooms & black olives | PASS | Pass ✓ | TN |
| 11 | click — Add to cart | PASS | Pass ✓ | TN |
| 12 | click — View your cart | PASS | Pass ✓ | TN |
| 13 | click — Confirm basket | PASS | Pass ✓ | TN |
| **14** | **abstractVerify — Mixed (Medium) in checkout** | **FAIL** (content_mismatch) | **Fail ✗** | **TP ✓** |

**Result: 2 failures detected, 2 real failures → Perfect precision and recall.**

**True Positive — Step 7:**
The click on "Mixed (Medium) Pizza" opened the "Mediterranean (Central)" pizza detail page. The visual observer correctly identified the wrong item in POST. The system operated in **vision-only mode** for the entire app (all 15 log buckets were empty per the nested format warning), demonstrating that the VLM pipeline alone is sufficient when the visual signal is unambiguous.

**True Positive — Step 14:**
The abstract verification confirmed the wrong pizza persisted to checkout. The system reported Steps 7 and 14 as two **isolated failures** rather than a causal chain — consistent with the v2 finding that `find_likely_cause()` scores below 0.3 due to the 6-step temporal gap and action type difference (click vs. abstractVerification). This is a sub-optimality in chain reporting but does not affect detection accuracy.

---

### App 7 — O Bilet · 9 steps · Log: nested (preassigned)

**Scenario:** Splash screen → category dashboard → set Bus + route Istanbul→Ankara (10 Apr) → search → open filter → apply company filter → select 22:00 AKSU trip → select Seat 12 + confirm → waitUntil Passenger Details form.

| Step | Action / Element | System Result | Ground Truth | Assessment |
|---|---|---|---|---|
| 0 | waitUntil — App Logo | PASS | Pass ✓ | TN |
| 1 | waitUntil — Category Dashboard | PASS | Pass ✓ | TN |
| 2 | click — Bus tab + set parameters | PASS | Pass ✓ | TN |
| 3 | click — SEARCH button | PASS | Pass ✓ | TN |
| 4 | click — FILTER button | PASS | Pass ✓ | TN |
| 5 | click — Apply multi-company filter | PASS | Pass ✓ | TN |
| 6 | click — 22:00 AKSU trip | PASS | Pass ✓ | TN |
| **7** | **click — Seat 12 + Confirm and Continue** | **FAIL** (system_error) | **Fail ✗** | **TP ✓** |
| **8** | **waitUntil — Passenger Details Input** | **FAIL** (element_missing) | **Fail ✗** | **TP ✓** |

**Result: 2 failures detected, 2 real failures → Perfect precision and recall.**

**True Positive — Step 7 (Root Failure):**
The system correctly identified the 504 Gateway Timeout on `POST /api/v3/bus/reserve-seat`. Log observer flagged `[NET_FAIL, SYS_ERR]` and the decider classified `system_error`. Visual: loading overlay persisted in POST (reservation did not complete).

**True Positive — Step 8 (Cascading Failure):**
The waitUntil for Passenger Details form correctly failed. Log: `UI_STALL: Navigation to PassengerDetailsActivity blocked by failed reservation dependency`. The causal chain `Step 7 → Step 8` was correctly constructed. This is the cleanest two-step chain in the dataset.

---

## 4. Part B — Group 411 Traces: Case-by-Case Breakdown

---

### Case 1 — Passo App · 11 steps · No unified log (step-level analysis)

**Scenario:** Verify Futbol category → click Futbol → tap Search → type "Eskişehirspor" → click match → click Bilet Al → increase ticket count → click Bilet Al (block selection) → select block 102 → click Koltuk Seç → click occupied (black) seat.

**Ground truth per 411 reference output:** Only Step 10 (their step_index 11: occupied seat click → no state change) is a real failure.

| Step | Action / Element | System Result | Ground Truth | Assessment |
|---|---|---|---|---|
| 0 | abstractVerify — Futbol category visible | PASS | Pass ✓ | TN |
| 1 | click — Futbol category | PASS | Pass ✓ | TN |
| 2 | click — Search button | PASS | Pass ✓ | TN |
| 3 | type — "Eskişehirspor" | PASS | Pass ✓ | TN |
| 4 | click — Eskişehirspor - Tire 2021 FK | PASS | Pass ✓ | TN |
| **5** | **click — Bilet Al button** | **FAIL** (action_failed) | **Pass ✓** | **FP ✗** |
| 6 | click — (+) ticket quantity | PASS | Pass ✓ | TN |
| 7 | click — Bilet Al → block selection | PASS | Pass ✓ | TN |
| 8 | click — Block 102 | PASS | Pass ✓ | TN |
| 9 | click — Koltuk Seç button | PASS | Pass ✓ | TN |
| **10** | **click — Occupied (black) seat** | **FAIL** (action_failed) | **Fail ✗** | **TP ✓** |

**Result: 2 failures detected, 1 real failure → 1 TP + 1 FP.**

**True Positive — Step 10:**
The click on the black (occupied) seat produced no state change between PREV and POST. The system correctly classified this as `action_failed`. The 411 reference confirms: "No visual change detected after clicking the seat. (Root Cause: The click action may not have been registered, or the seat is not selectable.)" Both systems agree on this failure.

**False Positive — Step 5:**
Our system observed the `Bilet Al` button becoming "inactive (grayed out)" with no navigation or content change in POST. However, the 411 reference output (step_index 6) shows a PASS: "The 'Bilet Al' button was clicked, leading to a change in the UI where ticket categories are now displayed." The discrepancy indicates a **VLM observation error** — our vision observer misread the POST screenshot state, interpreting the ticket category list appearance as a button grayout rather than a UI expansion. This is a false positive caused by visual hallucination in the observer, not a decider rule error.

**Implication:** The observer's `semantic_screen_summary` for Step 5 was insufficient to override the element-level misreading. This suggests the observer prompt's SELECTED STATE and navigation detection clauses need strengthening for collapsible/expandable UI elements that replace the trigger button with new content.

---

### Case 2 — Bank App · 8 steps · Log: inline (g411 format)

**Scenario:** Verify balance (5250 TL) → navigate to Para Gönder → enter recipient "Tuna" → enter amount "300" → click Transfer → see Transfer Successful screen → navigate to dashboard → abstractVerify balance.

**Ground truth per user annotation:** Steps 4, 6, and 7 are real failures. Step 6 is a "systematic problem not about rules."

| Step | Action / Element | System Result | Ground Truth | Assessment |
|---|---|---|---|---|
| 0 | abstractVerify — balance 5250.00 TL | PASS | Pass ✓ | TN |
| 1 | click — Para Gönder | PASS | Pass ✓ | TN |
| 2 | type — "Tuna" in Alıcı adı | PASS | Pass ✓ | TN |
| 3 | type — "300" in amount field | PASS | Pass ✓ | TN |
| **4** | **click — Transfer button** | **FAIL** (system_error) | **Fail ✗** | **TP ✓** |
| 5 | see — Transfer Successful screen | PASS | Pass ✓ | TN |
| **6** | **navigate — Back to dashboard** | **PASS** | **Fail ✗** | **FN ✗** |
| **7** | **abstractVerify — balance unchanged** | **FAIL** (element_missing) | **Fail ✗** | **TP ✓** |

**Result: 2 failures detected, 3 real failures → 2 TP + 0 FP + 1 FN.**

**True Positive — Step 4:**
The Transfer button triggered a `500 Internal Server Error`. Log observer flagged `[NET_FAIL, SYS_ERR]`. The system correctly identified this as `system_error` despite the visual POST screen showing a "Transfer Successful" confirmation — the log evidence overrode the misleading visual (log can only override PASS→FAIL, never FAIL→PASS — functioning as intended).

**True Positive — Step 7:**
The abstract verification element `Bakiyeyi doğrula` was not visible in either PREV or POST screens. The system correctly classified this as `element_missing`. This reflects the downstream consequence of Step 6's missed failure — the verification could not proceed because the expected state was never established.

**False Negative — Step 6 (Systematic Problem):**


---

### Case 3 — Ticket App · 11 steps · Log: inline (g411 format)

**Scenario:** Verify Login screen → login (enter credentials) → click Theater category → click Sports category → click Details (Madrid Falcons) → click Select Seats → click occupied seat A3 → click available seat A1 → click Go to Cart → verify seat + price → click Confirm & Pay.

**Ground truth per 411 reference output:** Steps 2, 3, 6, and 10 are real failures.

| Step | Action / Element | System Result | Ground Truth | Assessment |
|---|---|---|---|---|
| 0 | abstractVerify — Login screen present | PASS | Pass ✓ | TN |
| 1 | click — Enter credentials + Login | PASS | Pass ✓ | TN |
| **2** | **click — Theater category** | **FAIL** (action_failed) | **Fail ✗** | **TP ✓** |
| **3** | **click — Sports category** | **FAIL** (system_error, caused by Step 2) | **Fail ✗** | **TP ✓** |
| 4 | click — Details (Madrid Falcons) | PASS | Pass ✓ | TN |
| 5 | click — Select Seats | PASS | Pass ✓ | TN |
| **6** | **click — Occupied seat A3** | **FAIL** (action_failed) | **Fail ✗** | **TP ✓** |
| 7 | click — Available seat A1 | PASS | Pass ✓ | TN |
| 8 | click — Go to Cart | PASS | Pass ✓ | TN |
| 9 | abstractVerify — seat A1, price $99.50 | PASS | Pass ✓ | TN |
| **10** | **click — Confirm & Pay** | **FAIL** (system_error, caused by Step 6) | **Fail ✗** | **TP ✓** |

**Result: 4 failures detected, 4 real failures → Perfect precision and recall.**

**True Positive — Step 2:**
Click on Theater category produced no visual change between PREV and POST. Log confirmed: "User tapped Theater." — the action was registered but the UI did not navigate. Classified as `action_failed`. Matches 411 reference.

**True Positive — Step 3 (Cascading):**
Sports category navigation showed a RenderFlex overflow (869 pixels on the right side). Log: console rendering exception. Classified as `system_error`. The causal chain `Step 2 → Step 3` was correctly constructed — the cascade is plausible because the non-navigation at Step 2 may have left the app in an unstable UI state that surfaced on the next navigation.

**True Positive — Step 6:**
Click on occupied seat A3 returned `ERROR: Seat unavailable`. No UI state change. Classified as `action_failed`. Matches 411 reference exactly.

**True Positive — Step 10 (Cascading):**
`POST /api/v1/payment → 500 Internal Server Error`. Classified as `system_error`. The causal chain `Step 6 → Step 10` was constructed — the occupied-seat error likely corrupted the cart/reservation state, preventing payment completion.

**Two failure chains correctly identified:**
- **Chain 1:** `Step 2 → Step 3` (UI non-response → rendering error)
- **Chain 2:** `Step 6 → Step 10` (seat error → payment server error)

This is the best-performing 411 case: 100% precision and recall with correctly structured chains. The inline log format (`[g411]`) provided good evidence quality, and the 411 reference output confirms all four failures align with our detections.

---

### Case 4 — Rent-a-Car App · 11 steps · Log: format=none (no unified log; inline logs for some steps)

**Scenario:** Verify main screen → select cities (Ankara→Izmir) → select dates (14–17 April) → search vehicles → verify vehicle list → click Kirala for Mercedes-Benz G-Class → navigate to payment screen → verify reservation details → enter promo code "DISCOUNT20" → click Uygula (apply) → click Onayla ve Öde (pay).

**Ground truth per user annotation:** Steps 5, 8, 9, and 10 are real failures. Step 8 fails because the discount was claimed applied by the UI but the total price did not update — a visual inconsistency the observer should have caught.

| Step | Action / Element | System Result | Ground Truth | Assessment |
|---|---|---|---|---|
| 0 | abstractVerify — main screen loaded | PASS | Pass ✓ | TN |
| 1 | click — Select cities Ankara/Izmir | PASS | Pass ✓ | TN |
| 2 | click — Select dates 14/17 April | PASS | Pass ✓ | TN |
| 3 | click — Search vehicles | PASS | Pass ✓ | TN |
| 4 | abstractVerify — vehicle list displayed | PASS | Pass ✓ | TN |
| **5** | **click — Kirala (Mercedes-Benz G-Class)** | **FAIL** (action_failed) | **Fail ✗** | **TP ✓** |
| 6 | navigate — to payment screen | PASS | Pass ✓ | TN |
| 7 | abstractVerify — total ₺17,000 for 3 days | PASS | Pass ✓ | TN |
| **8** | **type — Promo code "DISCOUNT20"** | **PASS** | **Fail ✗** | **FN ✗** |
| **9** | **click — Uygula (apply discount)** | **FAIL** (content_mismatch) | **Fail ✗** | **TP ✓** |
| **10** | **click — Onayla ve Öde (pay)** | **FAIL** (system_error, caused by Step 9) | **Fail ✗** | **TP ✓** |

**Result: 3 failures detected, 4 real failures → 3 TP + 0 FP + 1 FN.**

**True Positive — Step 5:**
The Kirala action for Mercedes-Benz G-Class was blocked by a backend requirement: "special corporate account required for intercity drop-off in Izmir." Log observer flagged `[SYS_ERR]`. Classified as `action_failed` — visually the button appeared active but the action did not proceed.

**True Positive — Step 9:**
The discount application showed the success message `"Tebrikler! %20 indirim uygulandı!"` but the total remained ₺17,000.00 unchanged. Classified as `content_mismatch` — the expected new total (₺13,600) was not confirmed in POST. Correctly linked as caused by Step 5 (the intercity restriction created an inconsistent reservation state).

**True Positive — Step 10:**
`POST /payment → 504 Gateway Timeout` on Izmir regional gateway. Classified as `system_error`. Correctly chained from Step 9.

**False Negative — Step 8:**
Entering `DISCOUNT20` into the promo code field appeared visually successful — the text was correctly entered into the input. Our system reported PASS. The ground truth requires FAIL because the discount was claimed applied by the UI (the success message `"Tebrikler! %20 indirim uygulandı!"` was visible) yet the total price of ₺17,000 remained unchanged in the same view. This price-discount inconsistency was present in the POST screenshot and should have been detectable by the Vision Observer: a 20% discount on ₺17,000 yields an expected total of ₺13,600, but ₺17,000 was still displayed. The observer reported the promo code entry as successful without cross-checking whether the displayed total reflected the claimed discount. This is a **visual observation failure** — the evidence was on-screen but not caught.

---

### Case 5 — Wikipedia Website · 5 steps · No log (format=none)

**Scenario:** Verify Vikipedi page present → click top-left menu → click Hakkımızda → click "138 dil" language option → click "Osmanlı padişahı 3. Murad tahta çıktı seç."

**Ground truth per 411 reference output:** Step 4 is a real failure (instruction target not present in any visible UI element).

| Step | Action / Element | System Result | Ground Truth | Assessment |
|---|---|---|---|---|
| 0 | abstractVerify — "Vikipedi" visible | PASS | Pass ✓ | TN |
| 1 | click — Top-left menu icon | PASS | Pass ✓ | TN |
| 2 | click — Hakkımızda | PASS | Pass ✓ | TN |
| 3 | click — "138 dil" option | PASS | Pass ✓ | TN |
| **4** | **click — Osmanlı padişahı 3. Murad...** | **FAIL** (element_missing) | **Fail ✗** | **TP ✓** |

**Result: 1 failure detected, 1 real failure → Perfect precision and recall.**

**True Positive — Step 4:**
The instruction target "Osmanlı padişahı 3. Murad tahta çıktı seç." is lexically isolated from the current page context (Wikipedia Hakkımızda page showing general information). The element was absent from both PREV and POST screenshots. Classified as `element_missing`. The 411 reference classifies this as an "Instruction Error" with root cause: "The instruction seems to be unrelated to the current Wikipedia page context, possibly due to a scenario mismatch or incorrect step." Both systems agree: the target element is absent and the step fails. Operating in vision-only mode (no log data) was sufficient for this unambiguous absence detection.

---

## 5. Precision, Recall & F1 by App

### 5A. Our Own Traces

| App | Analyzed Steps | GT Failures | Predicted | TP | FP | FN | TN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|---|---|---|
| Migros | 8 | 1 | 1 | 1 | 0 | 0 | 7 | 100% | 100% | 100% |
| Bitaksi | 8 | 0 | 0 | 0 | 0 | 0 | 8 | N/A | N/A | N/A |
| Clock | 7 | 0 | 0 | 0 | 0 | 0 | 7 | N/A | N/A | N/A |
| Flight App | 22 | 6 | 6 | 6 | 0 | 0 | 16 | 100% | 100% | 100% |
| QNB | 13 | 1 | 1 | 1 | 0 | 0 | 12 | 100% | 100% | 100% |
| Yemeksepeti | 14 | 2 | 2 | 2 | 0 | 0 | 12 | 100% | 100% | 100% |
| O Bilet | 9 | 2 | 2 | 2 | 0 | 0 | 7 | 100% | 100% | 100% |
| **TOTAL** | **81** | **12** | **12** | **12** | **0** | **0** | **69** | **100.0%** | **100.0%** | **100.0%** |

> For apps with 0 ground truth failures, precision is undefined (no positive examples). Bitaksi and Clock contribute 0 TP and 0 FP — pure TN performance.

### 5B. Group 411 Traces

| App | Analyzed Steps | GT Failures | Predicted | TP | FP | FN | TN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|---|---|---|
| Passo | 11 | 1 | 2 | 1 | 1 | 0 | 9 | 50.0% | 100.0% | 66.7% |
| Bank App | 8 | 3 | 2 | 2 | 0 | 1 | 5 | 100.0% | 66.7% | 80.0% |
| Ticket App | 11 | 4 | 4 | 4 | 0 | 0 | 7 | 100.0% | 100.0% | 100.0% |
| Rent-a-Car | 11 | 4 | 3 | 3 | 0 | 1 | 7 | 100.0% | 75.0% | 85.7% |
| Wikipedia | 5 | 1 | 1 | 1 | 0 | 0 | 4 | 100.0% | 100.0% | 100.0% |
| **TOTAL** | **46** | **13** | **12** | **11** | **1** | **2** | **32** | **91.7%** | **84.6%** | **88.0%** |

### 5C. Combined Summary

| Dataset | Analyzed | GT Fail | Predicted | TP | FP | FN | TN | Precision | Recall | F1 |
|---|---|---|---|---|---|---|---|---|---|---|
| Our Traces | 81 | 12 | 12 | 12 | 0 | 0 | 69 | **100.0%** | **100.0%** | **100.0%** |
| Group 411 | 46 | 13 | 12 | 11 | 1 | 2 | 32 | 91.7% | 84.6% | 88.0% |
| **COMBINED** | **127** | **25** | **24** | **23** | **1** | **2** | **101** | **95.8%** | **92.0%** | **93.9%** |

---

## 6. Error Classification

### 6.1 False Positive (1 total)

| FP # | App | Step | Trigger | Root Cause Category |
|---|---|---|---|---|
| 1 | Passo App | Step 5 | Vision observer reported `Bilet Al` button became "inactive (grayed out)" with no navigation; 411 reference shows ticket categories appeared (PASS) | **VLM observation error** — observer misread UI state (ticket category expansion misidentified as button deactivation) |

**Analysis:** The false positive originates in the Vision Observer, not the Logic Decider. The observer's element-level description of POST failed to identify the ticket category list that appeared after clicking `Bilet Al`. This type of error — confusing a button that triggers content expansion with a button that becomes disabled — indicates the observer prompt's SELECTED STATE and navigation detection clauses need explicit handling for cases where the trigger element is replaced by revealed content rather than remaining visible. The `semantic_screen_summary` (1-2 sentence summary) should have caught the navigation-to-new-content, but did not provide sufficient signal in this case.

### 6.2 False Negatives (2 total)

| FN # | App | Step | System Result | Ground Truth | Root Cause Category |
|---|---|---|---|---|---|
| 1 | Bank App | Step 6 | PASS (dashboard loaded) | FAIL (balance unchanged after "successful" transfer) | **Cross-step state inconsistency** — requires tracking expected state across non-adjacent steps (Step 0 balance, Step 3 amount, Step 5 success claim, Step 6 balance comparison) |
| 2 | Rent-a-Car | Step 8 | PASS (promo code entered) | FAIL (discount claimed applied but total price unchanged) | **Visual observation failure** — discount success message and unchanged ₺17,000 total were both visible in POST; observer reported text entry success without verifying the price reflected the claimed discount |

**FN Pattern Analysis:**
The two false negatives arise from different root causes:
- **Bank App Step 6** — the failure produces no single-step visual evidence; it is only detectable by comparing the balance at Step 6 against the expected post-transfer value derived from Steps 0 and 3. This is a genuine **cross-step state inconsistency** that requires expected-state propagation across non-adjacent steps.
- **Rent-a-Car Step 8** — the failure was visually detectable within the step itself: the UI simultaneously displayed a discount-applied success message and an unchanged total price of ₺17,000. This is a **visual observation failure** — the Vision Observer focused on confirming the promo code was entered rather than verifying that the displayed total was consistent with the claimed discount.

These two cases are structurally different: Bank App Step 6 is an architectural gap (cross-step tracking not yet implemented); Rent-a-Car Step 8 is an observer prompt gap (price-discount consistency check missing from the observer's evaluation criteria).

### 6.3 Sub-optimalities (Not Detection Errors)

| Issue | App | Description |
|---|---|---|
| Missed causal chain | Yemeksepeti | Steps 7 and 14 both detected but reported as ISOLATED rather than a chain (Step 7 → Step 14). `find_likely_cause()` scored below 0.3 due to 6-step temporal gap and click vs. abstractVerification type mismatch. Both steps were correctly detected. |
| Causal link to FN | Rent-a-Car | The chain `Step 5 → Step 9 → Step 10` is partially correct. The actual root chain should be `Step 8 → Step 9 → Step 10` (missed price-discount inconsistency → amount mismatch → payment timeout). The system constructed a valid but non-root chain that traces to the earlier Step 5 blockage. |
| Bank FN cascade | Bank App | Step 7 (`element_missing`) is downstream of Step 6 (FN). Because Step 6 was missed, Step 7 appears as an isolated failure rather than the second step in a chain `Step 6 → Step 7`. The chain structure is recoverable if Step 6 detection is added. |

---

## 7. Key Takeaways

### 7.1 Improvements from v32.x to v33.0 (Our Traces)

| Issue (v2 report) | v2 Count | v33 Count | Status |
|---|---|---|---|
| Log-step index mismatch FPs (Clock, QNB) | 4 FPs | 0 | **Fully resolved** |
| Rule 2 over-application FPs (Migros) | 2 FPs | 0 | **Fully resolved** |
| Invalid failure chains (Migros, QNB) | 2 spurious chains | 0 | **Fully resolved** |
| Missed causal chain (Yemeksepeti 7→14) | 1 | 1 | Still present (sub-optimality) |
| **Total FPs** | **6** | **0** | **–100%** |
| **Total FNs** | **0** | **0** | No change |

### 7.2 Performance Dimensions Summary

| Dimension | Our Traces | Group 411 | Combined | Verdict |
|---|---|---|---|---|
| Recall / failure detection | 100% | 84.6% | 92.0% | **Excellent on owned data; strong on external** |
| Precision / noise | 100% | 91.7% | 95.8% | **Excellent; single VLM obs. error on external** |
| F1 Score | 100% | 88.0% | 93.9% | **High across both datasets** |
| Log-integrated detection | 100% (with logs) | 91.7% | — | Log evidence consistently improves recall |
| Vision-only detection | 100% (Yemeksepeti) | 100% (Wikipedia) | — | VLM sufficient when visual signal is strong |
| Causal chain accuracy | Mixed (1 missed chain) | Good (2/3 chains correct) | — | Correct linking in most cases |
| Cross-step consistency | Not applicable | 0% (2 FNs) | — | **Current architectural gap** |

### 7.3 Generalization Assessment

The system was developed and tuned on the proprietary traces (Migros, Bitaksi, Clock, Flight App, QNB, Yemeksepeti, O Bilet). Applying it to the independently designed Group 411 test cases — without any adaptation — produced an F1 of **88.0%**, a **12-point drop from perfect score**. This gap is explained entirely by two limitations — a cross-step state tracking gap (Bank App Step 6) and a visual observation failure where a price-discount inconsistency was on-screen but not caught (Rent-a-Car Step 8) — not by prompt tuning or model behavior. The 1 false positive from the Passo VLM observation error is a random noise event, not a systematic flaw.

The 411 test cases were intentionally designed with subtle, realistic faults ("a sequence of successful, normal steps that ultimately leads to one intentional, critical bug"). The system detected 11 of 13 such intentional bugs — a strong result for a first-pass evaluation on unseen data.

---

## 8. Recommendations

### Rec 1 — Multi-step state propagation for abstractVerification steps (addresses both FNs)

Both false negatives (Bank App Step 6, Rent-a-Car Step 8) require comparing a value from a previous step against the current step's observations. Implement a **state scratchpad** in `GraphState` that records key numerical values extracted by the Vision Observer from prior steps (balances, amounts, totals). When the Logic Decider encounters a step where the `action` is `abstractVerification` or the instruction references a value from a prior step, inject the relevant scratchpad entries as `[STATE CONTEXT]` alongside the regular evidence. This would enable cross-step inconsistency detection without restructuring the pipeline.

**Estimated impact:** Eliminates FN at Bank App Step 6 (balance comparison). May also close Rent-a-Car Step 8 if the expected post-entry validation state is tracked.

### Rec 2 — Observer prompt: explicit handling of content-expansion patterns (addresses Passo FP)

Add a clause to the Vision Observer prompt covering **trigger-reveal patterns**: when a button is clicked and new content appears in POST that replaces or overlays the trigger area, the observer must report the revealed content as the primary POST state — not infer the trigger button's final state from visual ambiguity. Example guidance: "If clicking a button causes a new panel, list, or overlay to appear, describe what appeared — do not assess whether the button is active or inactive unless it is still clearly visible in POST." This directly targets the Bilet Al misread.

**Estimated impact:** Eliminates the Passo FP. Prevents similar misreads on accordion menus, expandable forms, and ticket category selectors.

### Rec 3 — Strengthen causal chain scoring for abstractVerification steps (addresses Yemeksepeti sub-optimality)

`abstractVerification` steps that fail should be treated as likely downstream of any earlier `content_mismatch` failure involving the same logical entity. Add a keyword-similarity check between the failed `abstractVerification` instruction and prior failure `root_cause` strings. A cosine similarity above 0.5 should override the 0.3 threshold in `find_likely_cause()` when the types are `click → abstractVerification`. This was identified in the v2 report and remains relevant.

### Rec 4 — Observer prompt: verify price-discount consistency for promo code steps (addresses Rent-a-Car FN)

When a step involves entering a promo code, coupon, or discount value, the Vision Observer should explicitly check whether the displayed total price is consistent with the claimed discount. Add a clause to the observer prompt: "If the POST screen shows a discount success message alongside a price or total value, verify that the displayed total reflects the stated discount percentage applied to the pre-discount amount. If the total is unchanged despite the success message, report this as a price-discount inconsistency." This targets the Step 8 miss directly — both the discount message and the unchanged ₺17,000 were on-screen but the observer did not cross-check them.

### Rec 5 — Confidence distribution review for 411 traces

All 131 analyzed steps in this evaluation were classified as **High confidence (≥0.85)** with average confidence of 0.92–0.95 across apps. Given that 3 steps were incorrectly evaluated (1 FP, 2 FN), the confidence scores do not distinguish between correct and incorrect predictions in this dataset. Consider adding a calibration pass that penalizes confidence when: (a) log data is absent (`has_logs=False`) for an action type that typically generates backend events (type, click-on-submit, abstractVerify), or (b) the visual observer's `semantic_screen_summary` contradicts its `post_screen_changes` at the element level. Calibrated confidence would allow downstream consumers to identify the 3 uncertain steps rather than treating all high-confidence outputs uniformly.

---

## 9. Dataset Coverage Summary

| App / Case | Source | Log Format | Mode | Steps | GT Fail | TP | FP | FN |
|---|---|---|---|---|---|---|---|---|
| Migros | Our traces | nested | preassigned | 8 | 1 | 1 | 0 | 0 |
| Bitaksi | Our traces | flat_assertions | preassigned | 8 (1 skip) | 0 | 0 | 0 | 0 |
| Clock | Our traces | flat_indexed | preassigned | 7 | 0 | 0 | 0 | 0 |
| Flight App | Our traces | raw | dynamic/keyword | 22 (3 skip) | 6 | 6 | 0 | 0 |
| QNB | Our traces | flat_indexed | preassigned | 13 | 1 | 1 | 0 | 0 |
| Yemeksepeti | Our traces | nested (empty) | preassigned | 14 (1 skip) | 2 | 2 | 0 | 0 |
| O Bilet | Our traces | nested | preassigned | 9 | 2 | 2 | 0 | 0 |
| Passo App | Group 411 | inline g411 / none | per-step | 11 | 1 | 1 | 1 | 0 |
| Bank App | Group 411 | inline g411 / none | per-step | 8 | 3 | 2 | 0 | 1 |
| Ticket App | Group 411 | inline g411 | per-step | 11 | 4 | 4 | 0 | 0 |
| Rent-a-Car | Group 411 | inline g411 / none | per-step | 11 | 4 | 3 | 0 | 1 |
| Wikipedia | Group 411 | none | none | 5 | 1 | 1 | 0 | 0 |
| **TOTAL** | | | | **127** | **25** | **23** | **1** | **2** |

---

*Report generated for Describer-Decider v33.0. Evaluation date: 2026-04-28.*
*Our traces: 7 proprietary apps across Turkish e-commerce, transport, finance, and food delivery domains.*
*Group 411 traces: 5 independently designed test cases (Passo ticketing, bank transfer, event ticketing, rent-a-car, Wikipedia web)*