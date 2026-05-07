#!/bin/bash
# Set up Gerrit test environment: create users, project, push test commit
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_REPO="$PROJECT_DIR/gerrit_test_repo"

echo "=== Setting up Gerrit Test Environment ==="

# Wait for Gerrit to be ready
echo "Waiting for Gerrit to be ready..."
for i in $(seq 1 12); do
    if curl -s http://localhost:8080 > /dev/null 2>&1; then
        echo "Gerrit is ready!"
        break
    fi
    if [ "$i" -eq 12 ]; then
        echo "ERROR: Gerrit did not start. Check gerrit-start.sh"
        exit 1
    fi
    sleep 5
done

# Authenticate with Gerrit (DEVELOPMENT_BECOME_ANY_ACCOUNT mode)
AUTH_URL="http://localhost:8080/accounts/self"
curl -s -X PUT "$AUTH_URL/name" -H "Content-Type: application/json" -d '"Gerrit Admin"' > /dev/null
curl -s -X PUT "$AUTH_URL/email" -H "Content-Type: application/json" -d '"admin@gerrit.local"' > /dev/null
curl -s -X PUT "$AUTH_URL/password.git" -H "Content-Type: application/json" -d '"admin"' > /dev/null

echo "Admin user configured."

# Create test project
echo "Creating test project..."
curl -s -X PUT "http://localhost:8080/projects/test-project" \
    -H "Content-Type: application/json" \
    -d '{"description": "Test project for auto-review", "create_empty_commit": true}' > /dev/null

echo "Test project created: test-project"

# Create code-reviewer user
echo "Creating code-reviewer user..."
curl -s -X PUT "http://localhost:8080/accounts/code-reviewer" \
    -H "Content-Type: application/json" \
    -d '{"name": "Code Reviewer", "email": "reviewer@gerrit.local"}' > /dev/null
curl -s -X PUT "http://localhost:8080/accounts/code-reviewer/password.git" \
    -H "Content-Type: application/json" \
    -d '"reviewer"' > /dev/null
echo "code-reviewer user created."

# Clone test project and push a test commit
echo "Preparing test commit..."
rm -rf "$TEST_REPO"
git clone http://admin:admin@localhost:8080/test-project "$TEST_REPO"
cd "$TEST_REPO"

# Create a test commit with sample C code
cat > main.c << 'EOF'
#include <stdio.h>
#include <string.h>

#define BUFFER_SIZE 64

static char config_buffer[BUFFER_SIZE];

void parse_config(const char *input) {
    char temp[BUFFER_SIZE];
    strcpy(temp, input);
    sprintf(config_buffer, "config: %s", temp);
}

int main(void) {
    parse_config("test input");
    printf("Config: %s\n", config_buffer);
    return 0;
}
EOF

git add main.c
git commit -m "Add initial config parser implementation"
git push origin HEAD:refs/for/master

echo ""
echo "=== Test environment ready! ==="
echo "Web UI:   http://localhost:8080"
echo "Project:  test-project"
echo "Admin:    admin / admin"
echo "Reviewer: code-reviewer / reviewer"
echo ""
echo "To add code-reviewer to the change:"
echo "  ssh -p 29418 admin@localhost gerrit set-reviewers --add code-reviewer <change-id>"
echo ""
echo "Then run auto-review:"
echo "  python scripts/auto_review.py --change-id <change-id> --base-branch master"
