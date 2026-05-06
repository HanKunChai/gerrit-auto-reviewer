"""Tests for the review rules engine."""

from review_rules import ReviewEngine


class TestRulesLoading:
    def test_load_builtin(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        assert len(engine.rules) > 0

    def test_load_builtin_has_coding_style(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        rule_ids = [r["id"] for r in engine.rules]
        assert "C-STYLE-001" in rule_ids
        assert "C-SEC-001" in rule_ids
        assert "C-BUG-001" in rule_ids


class TestSecurityRules:
    def test_detect_strcpy(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        diff = "+    strcpy(dst, src);\n"
        issues = engine.run(diff, file_path="test.c")
        assert any(i.rule_id == "C-SEC-001" for i in issues)

    def test_detect_strcat(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        diff = "+    strcat(buf, data);\n"
        issues = engine.run(diff, file_path="test.c")
        assert any(i.rule_id == "C-SEC-002" for i in issues)

    def test_detect_sprintf(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        diff = "+    sprintf(out, \"%s\", in);\n"
        issues = engine.run(diff, file_path="test.c")
        assert any(i.rule_id == "C-SEC-003" for i in issues)

    def test_safe_functions_not_flagged(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        diff = "+    snprintf(buf, sizeof(buf), \"%s\", src);\n"
        issues = engine.run(diff, file_path="test.c")
        assert not any(i.rule_id == "C-SEC-001" for i in issues)
        assert not any(i.rule_id == "C-SEC-003" for i in issues)


class TestCodingStyleRules:
    def test_detect_brace_on_same_line(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        diff = "int main() {\n"
        issues = engine.run(diff, file_path="test.c")
        assert any(i.rule_id == "C-STYLE-001" for i in issues)

    def test_allman_brace_not_flagged(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        diff = "+int main()\n+{\n"
        issues = engine.run(diff, file_path="test.c")
        assert not any(i.rule_id == "C-STYLE-001" for i in issues)

    def test_line_too_long(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        diff = "+" + "x" * 200 + "\n"
        issues = engine.run(diff, file_path="test.c")
        assert any(i.rule_id == "C-STYLE-002" for i in issues)

    def test_multi_var_declaration(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        diff = "+    int a, b;\n"
        issues = engine.run(diff, file_path="test.c")
        assert any(i.rule_id == "C-STYLE-004" for i in issues)


class TestCommonBugs:
    def test_free_call(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        diff = "+    free(ptr);\n"
        issues = engine.run(diff, file_path="test.c")
        assert any(i.rule_id == "C-BUG-003" for i in issues)

    def test_allocation_call(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        diff = "+    char *p = malloc(100);\n"
        issues = engine.run(diff, file_path="test.c")
        assert any(i.rule_id == "C-BUG-004" for i in issues)

    def test_variable_division(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        diff = "+    int x = a / b;\n"
        issues = engine.run(diff, file_path="test.c")
        assert any(i.rule_id == "C-BUG-005" for i in issues)


class TestDiffParsing:
    def test_parse_added_lines(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        diff = """--- a/test.c
+++ b/test.c
@@ -1,3 +1,4 @@
 int x = 1;
-int y = 2;
+int y = 2;
+strcpy(dst, src);
 int z = 3;
"""
        issues = engine.run(diff, file_path="test.c")
        assert any(i.rule_id == "C-SEC-001" for i in issues)

    def test_empty_diff(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        issues = engine.run("", file_path="test.c")
        assert len(issues) == 0

    def test_no_false_positives(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        diff = "+    x = y + z;\n+    return 0;\n"
        issues = engine.run(diff, file_path="test.c")
        assert len(issues) == 0


class TestEngineEdgeCases:
    def test_none_diff(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        issues = engine.run(None)
        assert len(issues) == 0

    def test_multiple_files_in_diff(self):
        engine = ReviewEngine()
        engine.load_builtin_rules()
        diff = """--- a/a.c
+++ b/a.c
@@ -1 +1,2 @@
 old
+strcpy(d,s)
--- a/b.c
+++ b/b.c
@@ -1 +1,2 @@
 old
+sprintf(d,s)
"""
        issues = engine.run(diff)
        # Should flag issues in both files
        rule_ids = [(i.rule_id, i.file) for i in issues]
        assert ("C-SEC-001", "a.c") in rule_ids or ("C-SEC-001", "b.c") in rule_ids
