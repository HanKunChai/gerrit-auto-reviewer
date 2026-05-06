#!/bin/bash
# Run 5 test reviews and output logs
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
REVIEWS_DIR="$PROJECT_DIR/reviews"
TIMESTAMP=$(date +%Y%m%d_%H%M%S)
LOG_FILE="$REVIEWS_DIR/review-session-$TIMESTAMP.log"

mkdir -p "$REVIEWS_DIR"

echo "==========================================" | tee -a "$LOG_FILE"
echo "  Gerrit Auto-Review: 5 Test Commits" | tee -a "$LOG_FILE"
echo "  Started: $(date)" | tee -a "$LOG_FILE"
echo "==========================================" | tee -a "$LOG_FILE"
echo "" | tee -a "$LOG_FILE"

cd "$PROJECT_DIR"

# List of branches to review
BRANCHES=(
    "fix-strcpy:Branch 1/5: Buffer overflow (strcpy)"
    "fix-style:Branch 2/5: Style violations (brace, init, multi-var)"
    "fix-injection:Branch 3/5: Command injection (sprintf + system)"
    "fix-null:Branch 4/5: Null pointer deref + memory leak"
    "fix-gets:Branch 5/5: Unsafe gets() usage"
)

TOTAL_ISSUES=0
TOTAL_ERRORS=0

for entry in "${BRANCHES[@]}"; do
    BRANCH="${entry%%:*}"
    DESC="${entry#*:}"

    echo "" | tee -a "$LOG_FILE"
    echo "--- $DESC ---" | tee -a "$LOG_FILE"
    echo "" | tee -a "$LOG_FILE"

    # Run review
    python scripts/auto_review.py \
        --mock \
        --change-id "$BRANCH" \
        --revision 1 \
        --base-branch base \
        --output "$REVIEWS_DIR/${BRANCH}_${TIMESTAMP}.json" 2>&1 | tee -a "$LOG_FILE"

    # Extract stats
    if [ -f "$REVIEWS_DIR/${BRANCH}_${TIMESTAMP}.json" ]; then
        ISSUES=$(python -c "
import json
with open('$REVIEWS_DIR/${BRANCH}_${TIMESTAMP}.json') as f:
    d = json.load(f)
rules = d.get('rules', {})
issues = rules.get('issues', [])
errors = rules.get('errors', 0)
warnings = rules.get('warnings', 0)
print(f'{len(issues)}|{errors}|{warnings}')
for i in issues:
    print(f'  [{i[\"severity\"]}] {i[\"rule_id\"]}: {i[\"file\"]}:{i[\"line\"]} {i[\"message\"][:80]}')
" 2>&1)
        STATS=$(echo "$ISSUES" | head -1)
        IFS='|' read -r COUNT ERRS WARNS <<< "$STATS"
        echo "" | tee -a "$LOG_FILE"
        echo "$ISSUES" | tail -n +2 | tee -a "$LOG_FILE"
        echo "" | tee -a "$LOG_FILE"
        echo "  Result: $COUNT issues ($ERRS errors, $WARNS warnings)" | tee -a "$LOG_FILE"
        TOTAL_ISSUES=$((TOTAL_ISSUES + COUNT))
        TOTAL_ERRORS=$((TOTAL_ERRORS + ERRS))
    fi

    echo "" | tee -a "$LOG_FILE"
    echo "----------------------------------------" | tee -a "$LOG_FILE"
done

echo "" | tee -a "$LOG_FILE"
echo "==========================================" | tee -a "$LOG_FILE"
echo "  Review Session Complete" | tee -a "$LOG_FILE"
echo "  Total issues found: $TOTAL_ISSUES" | tee -a "$LOG_FILE"
echo "  Total errors: $TOTAL_ERRORS" | tee -a "$LOG_FILE"
echo "  Log: $LOG_FILE" | tee -a "$LOG_FILE"
echo "==========================================" | tee -a "$LOG_FILE"
