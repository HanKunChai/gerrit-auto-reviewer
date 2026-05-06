"""
Mock Gerrit REST API server for local testing.

Provides a Flask-based mock of the Gerrit REST API with predefined test
changes containing C code diffs suitable for testing the review system.
"""

import json
import threading
from typing import Optional

from flask import Flask, jsonify, request
from werkzeug.serving import make_server

# ---------------------------------------------------------------------------
# Test data
# ---------------------------------------------------------------------------

GERRIT_PREFIX = ")]}'\n"

TEST_CHANGES = {
    "Ideadbeef1234": {
        "id": "test-project~main~Ideadbeef1234",
        "change_id": "Ideadbeef1234",
        "subject": "Fix buffer overflow in config parser",
        "status": "OPEN",
        "project": "test-project",
        "branch": "main",
        "created": "2025-01-15 10:00:00.000000000",
        "updated": "2025-01-15 12:30:00.000000000",
        "_number": 1,
        "owner": {
            "name": "Developer One",
            "email": "dev1@example.com",
            "_account_id": 1000000,
        },
        "current_revision": "deadbeef1234",
        "revisions": {
            "deadbeef1234": {
                "_number": 1,
                "ref": "refs/changes/01/1/1",
                "commit": {
                    "message": "Fix buffer overflow in config parser\n\nReplace unsafe sprintf calls with snprintf\nand add new helper function.",
                    "parents": [{"commit": "abcdef9876"}],
                },
            }
        },
        "labels": {
            "Code-Review": {
                "all": [],
                "values": {
                    "-2": "Do not submit",
                    "-1": "I would prefer that you didn't submit",
                    " 0": "No score",
                    "+1": "Looks good, but someone else must approve",
                    "+2": "Looks good to me",
                },
            }
        },
    },
    "Ideadbeef5678": {
        "id": "test-project~main~Ideadbeef5678",
        "change_id": "Ideadbeef5678",
        "subject": "Refactor main loop and fix style issues",
        "status": "OPEN",
        "project": "test-project",
        "branch": "main",
        "created": "2025-01-16 09:00:00.000000000",
        "updated": "2025-01-16 14:00:00.000000000",
        "_number": 2,
        "owner": {
            "name": "Developer Two",
            "email": "dev2@example.com",
            "_account_id": 1000001,
        },
        "current_revision": "deadbeef5678",
        "revisions": {
            "deadbeef5678": {
                "_number": 1,
                "ref": "refs/changes/02/2/1",
                "commit": {
                    "message": "Refactor main loop and fix style issues\n\nApply Allman brace style, add function comments,\nand fix missing variable initializations.",
                    "parents": [{"commit": "abcdef9876"}],
                },
            }
        },
        "labels": {
            "Code-Review": {
                "all": [],
                "values": {
                    "-2": "Do not submit",
                    "-1": "I would prefer that you didn't submit",
                    " 0": "No score",
                    "+1": "Looks good, but someone else must approve",
                    "+2": "Looks good to me",
                },
            }
        },
    },
}

CHANGE_FILES = {
    "Ideadbeef1234": {
        "deadbeef1234": {
            "/COMMIT_MSG": {
                "status": "A",
                "lines_inserted": 7,
                "size_delta": 382,
                "size": 382,
            },
            "src/config.c": {
                "status": "M",
                "lines_inserted": 9,
                "lines_deleted": 3,
                "size_delta": 187,
                "size": 1823,
            },
        }
    },
    "Ideadbeef5678": {
        "deadbeef5678": {
            "/COMMIT_MSG": {
                "status": "A",
                "lines_inserted": 9,
                "size_delta": 450,
                "size": 450,
            },
            "src/main.c": {
                "status": "M",
                "lines_inserted": 18,
                "lines_deleted": 8,
                "size_delta": 320,
                "size": 2100,
            },
        }
    },
}

CHANGE_PATCHES = {
    "Ideadbeef1234": {
        "deadbeef1234": (
            "diff --git a/src/config.c b/src/config.c\n"
            "index abc123..def456 100644\n"
            "--- a/src/config.c\n"
            "+++ b/src/config.c\n"
            "@@ -10,17 +10,22 @@ static char config_buf[256];\n"
            " \n"
            "-void read_config(const char *filename)\n"
            "+void read_config(const char *filename)\n"
            " {\n"
            "     FILE *fp;\n"
            " \n"
            "     fp = fopen(filename, \"r\");\n"
            "     if (!fp) {\n"
            "-        sprintf(config_buf, \"config not found\");\n"
            "+        snprintf(config_buf, sizeof(config_buf), \"config not found\");\n"
            "         return;\n"
            "     }\n"
            "-    sprintf(config_buf, \"%s\", filename);\n"
            "+    snprintf(config_buf, sizeof(config_buf), \"%s\", filename);\n"
            "     fclose(fp);\n"
            " }\n"
            "+\n"
            "+/*\n"
            "+ * get_config_value - look up a key in config_buf\n"
            "+ * @key: the configuration key to look up\n"
            "+ *\n"
            "+ * Return: the integer value associated with @key, or 0 if not found.\n"
            "+ */\n"
            "+int get_config_value(const char *key)\n"
            "+{\n"
            "+    /* TODO: implement actual lookup */\n"
            "+    return 0;\n"
            "+}\n"
        )
    },
    "Ideadbeef5678": {
        "deadbeef5678": (
            "diff --git a/src/main.c b/src/main.c\n"
            "index fed321..cba654 100644\n"
            "--- a/src/main.c\n"
            "+++ b/src/main.c\n"
            "@@ -1,23 +1,35 @@\n"
            " #include <stdio.h>\n"
            " \n"
            "-void do_thing() {\n"
            "-    printf(\"hello\\n\");\n"
            "+/**\n"
            "+ * do_thing - prints a greeting message\n"
            "+ */\n"
            "+void do_thing(void)\n"
            "+{\n"
            "+    printf(\"hello, world\\n\");\n"
            " }\n"
            " \n"
            "-int add(int a, int b) {\n"
            "+int add(int a, int b)\n"
            "+{\n"
            "     return a + b;\n"
            " }\n"
            " \n"
            "-void loop() {\n"
            "+/**\n"
            "+ * loop - iterates and prints counts\n"
            "+ */\n"
            "+void loop(void)\n"
            "+{\n"
            "     int i;\n"
            "-    for(i=0;i<10;i++) {\n"
            "-        printf(\"%d\\n\",i);\n"
            "+    for (i = 0; i < 10; i++) {\n"
            "+        printf(\"count: %d\\n\", i);\n"
            "     }\n"
            " }\n"
            "+\n"
            "+static int counter = 0;\n"
            "+int increment(void)\n"
            "+{\n"
            "+    counter++;\n"
            "+    return counter;\n"
            "+}\n"
        )
    },
}

# Maps revision labels used at query time -> canonical revision hash
REVISION_MAP = {
    "Ideadbeef1234": {"current": "deadbeef1234", "rev1": "deadbeef1234"},
    "Ideadbeef5678": {"current": "deadbeef5678", "rev1": "deadbeef5678"},
}

# ---------------------------------------------------------------------------
# Flask app
# ---------------------------------------------------------------------------

app = Flask(__name__)


@app.after_request
def add_cors_headers(response):
    """Add CORS headers for local development."""
    response.headers["Access-Control-Allow-Origin"] = "*"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type,Authorization"
    response.headers["Access-Control-Allow-Methods"] = "GET,POST,PUT,DELETE,OPTIONS"
    return response


def _gerrit_json(data):
    """Return data prefixed with the Gerrit magic JSON prefix."""
    return GERRIT_PREFIX + json.dumps(data) + "\n"


def _resolve_revision(change_id, revision_id):
    """Resolve a revision label to a canonical revision hash."""
    rev_map = REVISION_MAP.get(change_id, {})
    return rev_map.get(revision_id, revision_id)


# -- Endpoints ----------------------------------------------------------------

@app.route("/changes/", methods=["GET", "OPTIONS"])
def list_changes():
    """Return open changes optionally filtered by query parameters."""
    if request.method == "OPTIONS":
        return "", 204

    status_filter = request.args.get("q", "")
    limit = request.args.get("n", 10, type=int)

    changes = list(TEST_CHANGES.values())

    if "status:open" in status_filter or "status:opened" in status_filter:
        changes = [c for c in changes if c["status"] == "OPEN"]
    elif "status:closed" in status_filter or "status:merged" in status_filter:
        changes = [c for c in changes if c["status"] != "OPEN"]

    if limit and limit > 0:
        changes = changes[:limit]

    return app.response_class(
        response=_gerrit_json(changes),
        status=200,
        mimetype="application/json",
    )


@app.route("/changes/<change_id>", methods=["GET", "OPTIONS"])
def get_change(change_id):
    """Return a single change by its change_id."""
    if request.method == "OPTIONS":
        return "", 204

    # Support both bare change_id and Gerrit's project~branch~change_id form
    lookup = change_id
    if "~" in change_id:
        parts = change_id.split("~")
        lookup = parts[-1]

    change = TEST_CHANGES.get(lookup)
    if change is None:
        return app.response_class(
            response=_gerrit_json({"error": "Change not found"}),
            status=404,
            mimetype="application/json",
        )

    return app.response_class(
        response=_gerrit_json(change),
        status=200,
        mimetype="application/json",
    )


@app.route(
    "/changes/<change_id>/revisions/<revision_id>/files",
    methods=["GET", "OPTIONS"],
)
def list_files(change_id, revision_id):
    """Return the file list for a given revision."""
    if request.method == "OPTIONS":
        return "", 204

    lookup = change_id
    if "~" in change_id:
        lookup = change_id.split("~")[-1]

    rev_hash = _resolve_revision(lookup, revision_id)

    files = CHANGE_FILES.get(lookup, {}).get(rev_hash)
    if files is None:
        return app.response_class(
            response=_gerrit_json({"error": "Revision not found"}),
            status=404,
            mimetype="application/json",
        )

    return app.response_class(
        response=_gerrit_json(files),
        status=200,
        mimetype="application/json",
    )


@app.route(
    "/changes/<change_id>/revisions/<revision_id>/patch",
    methods=["GET", "OPTIONS"],
)
def get_patch(change_id, revision_id):
    """Return the unified diff for a revision."""
    if request.method == "OPTIONS":
        return "", 204

    lookup = change_id
    if "~" in change_id:
        lookup = change_id.split("~")[-1]

    rev_hash = _resolve_revision(lookup, revision_id)

    patch = CHANGE_PATCHES.get(lookup, {}).get(rev_hash)
    if patch is None:
        return app.response_class(
            response=_gerrit_json({"error": "Revision not found"}),
            status=404,
            mimetype="application/json",
        )

    return app.response_class(
        response=patch,
        status=200,
        mimetype="text/plain",
    )


@app.route(
    "/changes/<change_id>/revisions/<revision_id>/review",
    methods=["POST", "OPTIONS"],
)
def post_review(change_id, revision_id):
    """Post a review on a revision."""
    if request.method == "OPTIONS":
        return "", 204

    lookup = change_id
    if "~" in change_id:
        lookup = change_id.split("~")[-1]

    if lookup not in TEST_CHANGES:
        return app.response_class(
            response=_gerrit_json({"error": "Change not found"}),
            status=404,
            mimetype="application/json",
        )

    body = request.get_json(silent=True) or {}
    message = body.get("message", "")
    labels = body.get("labels", {})
    comments = body.get("comments", {})

    # Simulate storing the review
    result = {
        "labels": labels,
        "message": message,
        "comments": {fp: len(cl) for fp, cl in comments.items()},
    }

    return app.response_class(
        response=_gerrit_json(result),
        status=200,
        mimetype="application/json",
    )


# ---------------------------------------------------------------------------
# Lifecycle helpers
# ---------------------------------------------------------------------------

_server: Optional[make_server] = None
_thread: Optional[threading.Thread] = None


def run(host: str = "127.0.0.1", port: int = 8080) -> None:
    """Start the mock Gerrit API server in a background daemon thread.

    Parameters
    ----------
    host : str
        The interface to bind to (default 127.0.0.1).
    port : int
        The port to listen on (default 8080).
    """
    global _server, _thread

    if _server is not None:
        return  # already running

    _server = make_server(host, port, app, threaded=True)
    _thread = threading.Thread(target=_server.serve_forever, daemon=True)
    _thread.start()


def stop() -> None:
    """Shut down the mock Gerrit API server."""
    global _server, _thread

    if _server is not None:
        _server.shutdown()
        _server = None
    _thread = None


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    run()
    print("Mock Gerrit API server running on http://127.0.0.1:8080")
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        stop()
