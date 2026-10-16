from tools.audit_public_tree import audit


def test_service_tokens_are_rejected_without_printing_values(tmp_path):
    secret = "wkr_" + "a" * 43
    (tmp_path / "unsafe.py").write_text("value = '" + secret + "'")
    result = audit(tmp_path, ["unsafe.py"])
    assert result == [{"path": "unsafe.py", "line": 1, "category": "service_token"}]
    assert secret not in str(result)


def test_private_key_is_rejected_even_in_auditor_file(tmp_path):
    path = tmp_path / "tools/audit_public_tree.py"
    path.parent.mkdir()
    path.write_text("-----BEGIN OPENSSH PRIVATE KEY-----\nsynthetic\n")
    assert audit(tmp_path, ["tools/audit_public_tree.py"])[0]["category"] == "private_key"


def test_private_filename_is_rejected_before_reading(tmp_path):
    assert audit(tmp_path, ["reports/output.json"])[0]["category"] == "private_file"
    assert audit(tmp_path, ["auth.json"])[0]["category"] == "private_file"


def test_documentation_addresses_and_format_checks_are_allowed(tmp_path):
    (tmp_path / "safe.py").write_text('host = "203.0.113.10"\nmarker = b"-----BEGIN OPENSSH PRIVATE KEY-----"\n')
    assert audit(tmp_path, ["safe.py"]) == []
