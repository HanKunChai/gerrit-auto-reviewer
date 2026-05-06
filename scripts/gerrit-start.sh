#!/bin/bash
# Start local Gerrit instance for testing
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
PROJECT_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
TOOLS_DIR="$PROJECT_DIR/tools"
GERRIT_SITE="$PROJECT_DIR/gerrit_site"

# Find Java
if [ -d "$TOOLS_DIR/jdk17" ]; then
    JAVA_HOME="$TOOLS_DIR/jdk17"
    PATH="$JAVA_HOME/bin:$PATH"
fi

JAVA_VER=$(java -version 2>&1 | head -1)
echo "Java: $JAVA_VER"
echo "Using Gerrit WAR: $TOOLS_DIR/gerrit.war"
echo "Gerrit site: $GERRIT_SITE"

# Check prerequisites
if [ ! -f "$TOOLS_DIR/gerrit.war" ]; then
    echo "ERROR: gerrit.war not found in $TOOLS_DIR"
    echo "Run: cd $TOOLS_DIR && curl -L -o gerrit.war https://gerrit-releases.storage.googleapis.com/gerrit-3.10.0.war"
    exit 1
fi

# Initialize if not yet done
if [ ! -d "$GERRIT_SITE" ]; then
    echo "Initializing Gerrit site..."
    java -jar "$TOOLS_DIR/gerrit.war" init --batch -d "$GERRIT_SITE"

    # Configure for local testing
    cat >> "$GERRIT_SITE/etc/gerrit.config" << 'GERRITCONFIG'
[gerrit]
    canonicalWebUrl = http://localhost:8080/
[database]
    type = H2
    database = db/ReviewDB
[auth]
    type = DEVELOPMENT_BECOME_ANY_ACCOUNT
[receive]
    enableSignedPush = false
[user]
    name = Gerrit Test
    email = test@gerrit.local
[sendemail]
    enable = false
[sshd]
    listenAddress = *:29418
[httpd]
    listenUrl = http://*:8080/
[container]
    javaHome =
GERRITCONFIG

    echo "Gerrit site initialized."
fi

# Start Gerrit
echo "Starting Gerrit daemon..."
java -jar "$TOOLS_DIR/gerrit.war" daemon --console-log -d "$GERRIT_SITE" &
GERRIT_PID=$!
echo $GERRIT_PID > "$PROJECT_DIR/gerrit.pid"
echo "Gerrit started (PID: $GERRIT_PID)"
echo "Web UI: http://localhost:8080"
echo "SSH:   ssh://localhost:29418"

# Wait for startup
sleep 5
echo "Gerrit should be ready now."
