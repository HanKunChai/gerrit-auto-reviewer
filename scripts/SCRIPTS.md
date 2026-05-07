# Scripts Overview

This directory contains utility scripts for the Gerrit Auto Viewer project.

## setup-test-env.sh

Creates a local test Git repository for simulating Gerrit code reviews. The
repository produces meaningful diffs between a clean `base` branch and a
`feature` branch that introduces intentional coding issues.

### Prerequisites

- Git (available on the PATH)
- Bash shell environment (Git Bash on Windows, WSL, macOS/Linux terminal)

### Usage

From the project root directory:

```bash
bash scripts/setup-test-env.sh
```

### What It Does

1. Creates a fresh Git repository at `test-data/test-repo/`
2. Commits a `base` branch with clean, well-structured C code
3. Creates a `feature` branch branched off `base` and commits code with
   intentional issues suitable for code review practice
4. Switches back to the `base` branch so the repository is at a clean
   starting point

### Test Repository Structure

```
test-data/test-repo/
  src/
    main.c      - Entry point that uses the utils and device modules
    utils.h     - Utility function declarations
    utils.c     - Utility implementations (contains most issues in feature)
    device.h    - Device HAL declarations
    device.c    - Device HAL stubs (contains brace-style issues in feature)
```

### Intentional Issues in the Feature Branch

| #  | Issue                         | File       | Function          |
|----|-------------------------------|------------|-------------------|
| 1  | Allman brace violation        | utils.c    | format_greeting   |
| 2  | Allman brace violation        | utils.c    | log_event         |
| 3  | Allman brace violation        | device.c   | device_init       |
| 4  | Allman brace violation        | device.c   | device_shutdown   |
| 5  | Unsafe `strcpy` (no bounds)   | utils.c    | format_greeting   |
| 6  | Unsafe `sprintf` (no bounds)  | utils.c    | format_greeting   |
| 7  | Unsafe `strcat` (no bounds)   | utils.c    | log_event         |
| 8  | Uninitialized variable        | utils.c    | format_greeting   |
| 9  | Null pointer dereference risk | utils.c    | log_event         |
| 10 | Buffer overflow (strcpy)      | utils.c    | format_greeting   |
| 11 | Buffer overflow (strcat)      | utils.c    | log_event         |

### Using with Gerrit Auto Viewer

Once the test repository is set up, you can point the Gerrit Auto Viewer at
`test-data/test-repo` to practice review workflows:

```bash
# Inspect the diff that would be reviewed
cd test-data/test-repo
git diff base..feature

# List commits on the feature branch
git log base..feature --oneline
```

### Cleaning Up

To remove the test repository and start fresh, delete the directory and
re-run the setup script:

```bash
rm -rf test-data/test-repo
bash scripts/setup-test-env.sh
```
