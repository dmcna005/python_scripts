# MongoDB Atlas Spend — Executive Report

**Period analysed:** December 2025 – September 2026 (10 invoice months)
**Prepared:** 5 October 2026
**Source:** `output_invoices/cost_report.pdf`, the live Atlas backup policy, and 33,074 invoice line items

---

## 1. Executive summary

Atlas spend over the last ten months was **$1,398,725** ($139,872/month). The forecast for the next twelve months is **$1,638,183**.

**Support (35.4%)** and **Backup (26.6%)** make up **62% of the bill** — more than all compute and storage combined. Both are addressable without touching application capacity.

The live backup policy has now been reviewed and it is **materially more expensive than assumed**: hourly snapshots **every hour**, daily snapshots retained **30 days**, weekly for 9 weeks, monthly for 12 months — and every snapshot is **copied to two additional regions**, roughly tripling storage on CCP-PROD.

| Action | Annual saving (base case) | % of forecast |
|---|---:|---:|
| Snapshot retention/cadence change (Scenario B) | $332,334 | 20.3% |
| **+ reduce copy regions from two to one (Scenario C)** | **$410,119** | **25.0%** |
| + renegotiate support to a 40% blended rate | **$564,256** | **34.4%** |

**The single most valuable finding is that daily snapshots are retained for 30 days and replicated three times.** That one setting, not the hourly cadence and not PITR, is where the money is.

---

## 2. Where the money goes

### By service (10 months)

| Service | Spend | Share |
|---|---:|---:|
| Support | $494,677 | 35.4% |
| Backup | $372,230 | 26.6% |
| Clusters (compute) | $343,719 | 24.6% |
| Storage | $166,201 | 11.9% |
| Data Transfer | $21,169 | 1.5% |
| Premium Features / Stream Processing | $729 | 0.1% |
| **Total** | **$1,398,725** | **100%** |

### By project (10 months)

| Project | Spend | Share |
|---|---:|---:|
| CCP-PROD | $854,840 | 61.1% |
| CCP-PE | $333,898 | 23.9% |
| CCP-UAT | $82,744 | 5.9% |
| CCP-QA-DEV-INT | $80,759 | 5.8% |
| SDT-DEV | $29,575 | 2.1% |
| SDT-PE | $9,620 | 0.7% |
| SDT-PROD / SDT-UAT / SDT_Test | $7,035 | 0.5% |
| TTO-AI-CHALLENGE | $254 | 0.0% |

**CCP-PROD and CCP-PE carry 85% of the bill.** CCP-PE alone — a non-production private-endpoint footprint — is $333,898, including $118,171 of support and $71,216 of backup. It merits its own review.

---

## 3. Action 1 — Fix the backup policy (largest, fastest win)

### The live policy

| Frequency | Interval | Retention | Retained copies |
|---|---|---|---:|
| Hourly | every 1 hour | 2 days | 48 |
| Daily | 07:00 UTC | **30 days** | 30 |
| Weekly | Saturday | 9 weeks | 9 |
| Monthly | last day of month | 12 months | 12 |
| **Additional Snapshot Copies** | **ENABLED** | **Iowa (centralus) + Virginia (eastus)** | **×3 storage** |
| PITR | continuous | 7 days | — |

This is roughly **46% heavier than the Atlas default policy before replication**, and then multiplied by three.

### What the invoices confirm

Backup is **$372,230 over ten months ($446,676 annualised)**, almost entirely one meter — Cloud Backup Storage at $371,874 (36.9M GB-days @ ~$0.00994). Splitting it by region proves the replication cost:

| Region | Backup spend | Role |
|---|---:|---|
| Virginia-East2 (eastus2) | $166,967 | Primary for most clusters |
| Iowa (centralus) | $102,835 | Primary + copy target |
| Virginia (eastus) | $102,427 | **Copy target only** |

For **CCP-PROD** ($294,350 of backup, 79% of all backup spend), the split is almost exactly even thirds — $89,640 / $102,316 / $102,394 — meaning roughly **$196,233 over ten months ($235,480/year) is spent purely on the two additional region copies.**

### Correction on PITR

The continuous-backup (PITR oplog) meter totals **$355 over ten months**. Cutting PITR from 7 days to 48 hours is worth about **$304 per year**. It is good hygiene and should still be done, but it is not a savings lever — the earlier estimate correctly flagged this and the live policy does not change it.

### Modelled scenarios

Model: [backup_savings_model.py](backup_savings_model.py) → `output_invoices/backup_savings_scenarios.csv`.

*Method:* snapshot storage is incremental, so each retained copy adds only the data changed since the previous snapshot. Storage is modelled as a **retention index** = 1.0 full baseline + Σ(retained copies × delta size as a fraction of a full snapshot), with deltas of 1% hourly / 5% daily / 15% weekly / 25% monthly (base case, swept low–high). The live policy scores **7.33**. Copy regions multiply the result.

| Scenario | Policy | Index | Storage cut | Backup saving | Support follow-through | **Total annual** |
|---|---|---:|---:|---:|---:|---:|
| A. Drop hourly only | hourly off; daily 30d, weekly 9w, monthly 12m; 2 copies | 6.85 | 6.5% | $29,633 | $11,853 | **$41,487** |
| B. Retention cut | **24h cadence, daily 7d, weekly 4w, monthly 6m; 2 copies** | 3.45 | 52.9% | $237,381 | $94,952 | **$332,334** |
| **C. Recommended** | **Scenario B + one copy region instead of two** | 3.45 | 65.3% | $292,942 | $117,177 | **$410,119** |
| D. Non-production profile | 24h × 3d, weekly 2w, monthly 3m; no copies | 2.20 | 85.8% | $384,619 | $153,848 | **$538,467** |

Scenario C sensitivity: **$388,582 – $428,556 per year**. Scenario D is the profile to apply to non-production only, not estate-wide.

**Deleting hourly snapshots is worth only $41k — 10% of the opportunity.** Hourly deltas are small. The value is in **daily retention 30 days → 7 days**, **monthly 12 → 6**, and **dropping one copy region**.

### Why support savings come along for free

Support is charged as a percentage of consumption, so **every dollar removed from backup also removes support**. The model applies a conservative flat 40% (the marginal Tier 4 rate). In reality Scenario C's reduction of $24,412/month exceeds the ~$14,700/month sitting in Tier 4, so part of it scores at 50% — the true follow-through is closer to **$129k**, making the figures above conservative.

### Recommended rollout

1. **Non-production (CCP-UAT, CCP-QA-DEV-INT, SDT-\*): Scenario D.** Delete hourly, daily ×3 days, weekly ×2, monthly ×3, no cross-region copies. No change-control burden.
2. **CCP-PE: Scenario D.** $71,216 of backup on a non-production project does not justify production retention.
3. **CCP-PROD: Scenario C.** Daily ×7 days, weekly ×4, monthly ×6, PITR 48 hours, and **one** copy region rather than two.
4. **Sequence the copy-region change first.** Disabling one copy region is a single setting, carries no retention risk, and releases ~$118,000/year on its own.
5. **Validate on one cluster**, confirm the GB-days drop on the next invoice, then roll out.

### Risk and compliance gates

- **30-day daily retention → 7 days is the biggest single change.** Confirm no audit or regulatory requirement depends on restoring to an arbitrary day within the last month.
- Confirm 12-month monthly retention can be trimmed to 6 months.
- Confirm the DR posture tolerates one copy region instead of two (the primary region snapshot plus one remote copy still survives a full region loss).
- Confirm a 48-hour PITR window satisfies the documented RPO for CCP-PROD.
- Savings ramp over roughly one retention cycle as existing snapshots age out — expect the full run-rate benefit after about 30–60 days, with monthly snapshot savings taking up to 12 months.

---

## 4. Action 2 — Renegotiate support

Atlas Enterprise Support is billed as a declining percentage of consumption:

| Tier | Rate | Billed (10 months) |
|---|---:|---:|
| Tier 1 | 70.0% | $134,063 |
| Tier 2 | 60.0% | $113,818 |
| Tier 3 | 50.0% | $187,988 |
| Tier 4 | 40.0% | $58,808 |
| **Total** | **54.7% blended** | **$494,677** |

We pay **54.7 cents of support per dollar of consumption** (list value $581,973; we hold a ~15% discount). The rate, not the consumption, is the lever.

**Negotiation asks, in priority order:**

1. **Re-band the tiers.** Tier 3 alone costs $187,988 at 50%. Moving the Tier 3/Tier 4 threshold down is the lowest-friction ask.
2. **Flat blended rate** on committed spend instead of the tiered schedule.
3. **Right-size by environment.** $277,000 (56%) of support sits on non-production projects (CCP-PE, CCP-UAT, CCP-QA-DEV-INT, SDT-\*). Enterprise SLAs on dev/test is a defensible concession.
4. **Time it with the backup change.** Cutting consumption raises the blended support rate (the cheapest tier empties first), which strengthens the case for re-banding.

Applied to the post-optimisation consumption base of $767,091:

| Negotiated blended rate | Annual support cost | Additional saving |
|---|---:|---:|
| Current tier schedule | $460,973 | — |
| 45% | $345,191 | **$115,782** |
| **40%** | **$306,836** | **$154,137** |
| 35% | $268,482 | **$192,491** |

---

## 5. Twelve-month outlook

Forecast from a time-split model comparison (Ridge, RandomForest, ExtraTrees, GradientBoosting); ExtraTrees selected, test R² 0.99.

| Project | Forecast next 12M | With 10% buffer |
|---|---:|---:|
| CCP-PROD | $1,035,393 | $1,138,933 |
| CCP-PE | $360,923 | $397,015 |
| CCP-UAT | $98,705 | $108,576 |
| CCP-QA-DEV-INT | $96,781 | $106,459 |
| SDT-DEV | $29,861 | $32,847 |
| SDT-PE | $9,682 | $10,651 |
| All others | $6,837 | $7,521 |
| **Total** | **$1,638,183** | **$1,802,001** |

### Baseline versus optimised

| | Consumption | Support | **Total** | Saving |
|---|---:|---:|---:|---:|
| Baseline (no action) | $1,060,033 | $578,150 | **$1,638,183** | — |
| + Backup Scenario B | $822,652 | $483,198 | **$1,305,849** | $332,334 (20.3%) |
| + Backup Scenario C | $767,091 | $460,973 | **$1,228,064** | $410,119 (25.0%) |
| + Support at 45% | $767,091 | $345,191 | **$1,112,282** | $525,901 (32.1%) |
| **+ Support at 40%** | **$767,091** | **$306,836** | **$1,073,927** | **$564,256 (34.4%)** |

Combined rows apply the renegotiated rate to the reduced consumption base, so support savings are not double-counted.

---

## 6. Recommended decisions

| # | Decision | Owner | Value |
|---|---|---|---:|
| 1 | Reduce Additional Snapshot Copies from two regions to one | Platform / DBA | ~$118k/yr |
| 2 | Cut daily snapshot retention 30 → 7 days, monthly 12 → 6, weekly 9 → 4 weeks; delete the hourly unit | Platform / DBA | ~$292k/yr combined with #1 |
| 3 | Apply the non-production profile (Scenario D) to CCP-PE, CCP-UAT, CCP-QA-DEV-INT, SDT-\* | Platform / DBA | included above |
| 4 | Reduce PITR window 7 days → 48 hours | Platform / DBA | ~$0.3k/yr (hygiene) |
| 5 | Open support renegotiation targeting a 40% blended rate or re-banded tiers | Procurement | $154k/yr |
| 6 | Review the CCP-PE footprint — 24% of spend on non-production | Application owner | TBD |
| 7 | RTO/RPO and compliance sign-off before the retention cut | Risk / Compliance | Gate |

**Combined target: $564,256 per year — a 34.4% reduction against the $1.64M forecast.**

---

## 7. Supporting material

- Chart pack and tables: `output_invoices/cost_report.pdf`
- Consolidated figures: `output_invoices/final_cost_report.csv`
- Backup scenario model: [backup_savings_model.py](backup_savings_model.py) → `output_invoices/backup_savings_scenarios.csv`
- Data quality and EDA: `output_invoices/eda_report.txt`, `output_invoices/cleaning_log.csv`

**Methodology note:** savings are modelled from the live backup policy and billed GB-days, not quoted by MongoDB. The incremental-storage deltas (1%/5%/15%/25%) are industry-typical assumptions; the low–high band reflects their uncertainty. Validate on a single CCP-PROD cluster before committing these figures to a budget.
