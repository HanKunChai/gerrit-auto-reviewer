#!/bin/bash
#
# setup-test-env.sh
#
# Creates a local test git repository for simulating Gerrit code review.
# The repository contains a "base" branch with clean C code and a "feature"
# branch with intentional coding issues suitable for code review practice.
#
# Usage: bash scripts/setup-test-env.sh
#

set -e

# Determine project root (script lives in scripts/ relative to project root)
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
TEST_REPO_DIR="$PROJECT_ROOT/test-data/test-repo"

# ---------------------------------------------------------------------------
# Clean up any previous test repository
# ---------------------------------------------------------------------------
if [ -d "$TEST_REPO_DIR" ]; then
    echo "Removing existing test repository..."
    rm -rf "$TEST_REPO_DIR"
fi

echo "Creating test repository at $TEST_REPO_DIR"
mkdir -p "$TEST_REPO_DIR/src"

# ---------------------------------------------------------------------------
# Initialize a new git repository
# ---------------------------------------------------------------------------
cd "$TEST_REPO_DIR"
git init
git config user.email "test-reviewer@example.com"
git config user.name  "Test Reviewer"

# ---------------------------------------------------------------------------
# Create base branch with clean, well-structured C code
# ---------------------------------------------------------------------------

# --- src/main.c ---
cat <<'SRCEOF' > src/main.c
#include <stdio.h>
#include <string.h>
#include "utils.h"
#include "device.h"

int main(void)
{
    int ret;
    char buffer[256];
    const char *message = "Hello, Gerrit Review!";

    ret = device_init();
    if (ret != 0)
    {
        printf("Failed to initialize device\n");
        return 1;
    }

    ret = format_greeting(buffer, sizeof(buffer), message);
    if (ret != 0)
    {
        printf("Failed to format greeting\n");
        device_shutdown();
        return 1;
    }

    printf("%s\n", buffer);
    log_event("Application started successfully");

    device_shutdown();
    return 0;
}
SRCEOF

# --- src/utils.h ---
cat <<'SRCEOF' > src/utils.h
#ifndef UTILS_H
#define UTILS_H

#include <stddef.h>

int format_greeting(char *buffer, size_t size, const char *name);
int log_event(const char *message);

#endif
SRCEOF

# --- src/utils.c (base: clean implementation) ---
cat <<'SRCEOF' > src/utils.c
#include <stdio.h>
#include <string.h>
#include <time.h>
#include "utils.h"

int format_greeting(char *buffer, size_t size, const char *name)
{
    if (buffer == NULL || name == NULL)
    {
        return -1;
    }

    int result = snprintf(buffer, size, "Hello, %s! The time is %ld",
                          name, (long)time(NULL));
    if (result < 0 || (size_t)result >= size)
    {
        return -1;
    }

    return 0;
}

int log_event(const char *message)
{
    if (message == NULL)
    {
        return -1;
    }

    printf("[LOG] %s\n", message);
    return 0;
}
SRCEOF

# --- src/device.h ---
cat <<'SRCEOF' > src/device.h
#ifndef DEVICE_H
#define DEVICE_H

int device_init(void);
void device_shutdown(void);

#endif
SRCEOF

# --- src/device.c (base: clean implementation) ---
cat <<'SRCEOF' > src/device.c
#include <stdio.h>
#include "device.h"

int device_init(void)
{
    printf("Device initialized\n");
    return 0;
}

void device_shutdown(void)
{
    printf("Device shut down\n");
}
SRCEOF

# ---------------------------------------------------------------------------
# Commit on the default branch, then rename it to 'base'
# ---------------------------------------------------------------------------
git add src/
git commit -m "Initial commit: base implementation with clean coding practices

Provides a working skeleton project with proper Allman brace style,
NULL-pointer guards, and bounds-checked string formatting."

# Rename the default branch (master/main) to 'base'
CURRENT_BRANCH="$(git rev-parse --abbrev-ref HEAD)"
git branch -m "$CURRENT_BRANCH" base

# ---------------------------------------------------------------------------
# Create the feature branch with intentional issues
# ---------------------------------------------------------------------------
git checkout -b feature

# --- Modified src/utils.c (contains intentional code-review issues) ---
cat <<'SRCEOF' > src/utils.c
#include <stdio.h>
#include <string.h>
#include <time.h>
#include "utils.h"

int format_greeting(char *buffer, size_t size, const char *name) {
    /*
     * ISSUE: Allman brace violation - function brace is on the same line
     *        as the signature instead of on its own line.
     */

    int ret;
    /*
     * ISSUE: Uninitialized variable - 'ret' is declared but never assigned
     *        a value before being read in the condition below.
     */

    char temp[64];

    /*
     * ISSUE: Buffer overflow / unsafe function - strcpy() performs no
     *        bounds checking. If 'name' is longer than 63 characters the
     *        fixed-size temp buffer will be overflowed.
     * FIX:   Use strncpy() or snprintf() with the buffer size.
     */
    strcpy(temp, name);

    /*
     * ISSUE: Using the uninitialized variable 'ret' in a condition.
     *        Since 'ret' was never initialized its value is indeterminate.
     */
    if (ret == 0)
    {
        /*
         * ISSUE: Unsafe function - sprintf() does not perform bounds
         *        checking. If the formatted output exceeds the size of
         *        'buffer' a buffer overflow will occur.
         * FIX:   Use snprintf() instead.
         */
        sprintf(buffer, "Hello, %s! The time is %ld", name, (long)time(NULL));
    }
    else
    {
        buffer[0] = '\0';
    }

    return 0;
}

int log_event(const char *message) {
    /*
     * ISSUE: Allman brace violation.
     *
     * ISSUE: Potential null pointer dereference - no NULL check is
     *        performed on 'message'. If NULL is passed, strcpy() and
     *        strcat() below will dereference a null pointer and crash.
     * FIX:   Add 'if (message == NULL) return -1;' at the top.
     */

    char log[32];

    strcpy(log, "LOG: ");

    /*
     * ISSUE: Buffer overflow - strcat() appends to a fixed 32-byte buffer
     *        with no length limit. If the concatenated result exceeds 31
     *        characters (plus terminator) the buffer will overflow.
     * FIX:   Use strncat() or snprintf() with the buffer size.
     */
    strcat(log, message);

    printf("%s\n", log);
    return 0;
}
SRCEOF

# --- Modified src/device.c (Allman brace violations only) ---
cat <<'SRCEOF' > src/device.c
#include <stdio.h>
#include "device.h"

int device_init(void) {
    /*
     * ISSUE: Allman brace violation - brace on same line.
     */
    printf("Device initialized\n");
    return 0;
}

void device_shutdown(void) {
    /*
     * ISSUE: Allman brace violation - brace on same line.
     */
    printf("Device shut down\n");
}
SRCEOF

# ---------------------------------------------------------------------------
# Commit the feature branch
# ---------------------------------------------------------------------------
git add src/utils.c src/device.c
git commit -m "WIP: Add string-processing logic and device HAL stubs

Introduces intermediate string copying and a hardware-abstraction layer.
Known issues flagged for code review -- see inline comments in utils.c
and device.c."

# ---------------------------------------------------------------------------
# Switch back to the base branch so the repo is at a clean starting point
# ---------------------------------------------------------------------------
git checkout base

echo ""
echo "============================================"
echo "  Test repository created successfully!"
echo "============================================"
echo ""
echo "  Location:  $TEST_REPO_DIR"
echo ""
echo "  Branches:"
echo "    base    - Clean, well-structured C code"
echo "    feature - Code with intentional review issues"
echo ""
echo "  Issues introduced in 'feature':"
echo "    1. Allman brace style violations (utils.c, device.c)"
echo "    2. Unsafe functions: strcpy, strcat, sprintf (utils.c)"
echo "    3. Potential null pointer dereference (log_event in utils.c)"
echo "    4. Uninitialized variable (format_greeting in utils.c)"
echo "    5. Buffer overflow patterns (utils.c)"
echo ""
echo "  Quick start:"
echo "    cd \"$TEST_REPO_DIR\""
echo "    git diff base..feature"
echo "    git log --oneline --graph --all"
echo ""
