# QQQ/TQQQ Rotation Monitor

Automated signal monitor for a **QQQ → TQQQ rotation strategy** based on VIX levels and TQQQ drawdown from all-time high.

Runs daily after US market close. Sends Telegram alerts for two conditions:

| Signal | Trigger |
|---|---|
| 🟢 **ENTRY** | VIX > 40 AND TQQQ ≥ 50% below ATH  **OR**  28 < VIX ≤ 40 AND TQQQ ≥ 75% below ATH |
| 🔴 **EXIT** | TQQQ crosses **below** its 30-day MA (only fires once after an ENTRY, on the crossover day) |

State (in trade / above MA) is persisted in `state.json` and auto-committed by the workflow.

---

## Setup

### 1. Add GitHub Secrets

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value |
|---|---|
| `TELEGRAM_BOT_TOKEN` | Bot token from BotFather |
| `TELEGRAM_CHAT_ID` | Your Telegram chat ID |

### 2. The workflow runs automatically

Weekdays at **21:30 UTC** (after US market close). Trigger manually via **Actions → Run workflow** at any time.

---

## Signal logic

### ENTRY (fires once when first triggered)
| Condition | VIX | TQQQ below ATH |
|---|---|---|
| A | > 40 | ≥ 50% |
| B | 28 – 40 | ≥ 75% |

### EXIT (fires once on crossover, only if in a trade)
- TQQQ was **above** its 30-day MA yesterday
- TQQQ is **below** its 30-day MA today

After EXIT fires, the monitor resets and watches for the next ENTRY.

## Local testing

```bash
pip install -r requirements.txt
export TELEGRAM_BOT_TOKEN="your_token"
export TELEGRAM_CHAT_ID="your_chat_id"
python monitor.py
```
