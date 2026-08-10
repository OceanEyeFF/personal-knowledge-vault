"""Public citation/source sanitization regressions."""

from urllib.parse import quote

import pytest

from src.relations.citations import (
    build_entry_locator,
    is_local_reference,
    resolve_citation_source,
    sanitize_public_evidence,
    sanitize_public_source_url,
)


@pytest.mark.parametrize(
    "local_reference",
    [
        r"C:\Users\fixture\chat.html",
        r"\\fixture-server\share\chat.md",
        r"\Windows\System32\secret.txt",
        r"\??\C:\Windows\System32\secret.txt",
        "file:///C:/Users/fixture/chat.md",
        "file://fixture-server/share/chat.md",
        "file://localhost/C:/Users/fixture/chat.md",
    ],
)
def test_local_source_urls_are_cleared_and_fall_back_to_entry_resource(
    local_reference: str,
) -> None:
    assert is_local_reference(local_reference) is True
    assert sanitize_public_source_url(local_reference) == ""
    assert resolve_citation_source(7, source_url=local_reference) == (
        build_entry_locator(7)
    )


def test_nested_evidence_redacts_local_references_by_value() -> None:
    payload = {
        "knowledge_id": 7,
        "source_url": "file:///C:/Users/fixture/source.md",
        "source": r"C:\Users\fixture\source.md",
        "evidence_payload": {
            "raw_target": r"\\fixture-server\share\target.md",
            "origin": "file://fixture-server/share/origin.md",
            "nested": {"value": "/home/fixture/private.md"},
        },
    }

    sanitized = sanitize_public_evidence(payload)

    assert sanitized["source_url"] == ""
    assert sanitized["source"] == "pkv://entries/7"
    assert sanitized["evidence_payload"] == {
        "raw_target": "[redacted-local-reference]",
        "origin": "[redacted-local-reference]",
        "nested": {"value": "[redacted-local-reference]"},
    }


def test_nested_evidence_redacts_local_references_in_dynamic_keys() -> None:
    sanitized = sanitize_public_evidence(
        {
            r"\Windows\System32\private-tag": "first",
            r"\??\C:\private-tag": "second",
            "[redacted-local-reference]": "third",
        }
    )

    assert sanitized == {
        "[redacted-local-reference]": "first",
        "[redacted-local-reference]#2": "second",
        "[redacted-local-reference]#3": "third",
    }


def test_public_source_url_removes_userinfo_redacts_encoded_query_and_fragment() -> None:
    raw_url = (
        "HTTPS://url-user:URL-PASSWORD@example.com:443/private/path"
        ";%74oken=MATRIX-SECRET;safe=matrix-value"
        "?safe=value&%61PI%5FKey=QUERY-SECRET"
        "&X-Amz-Signature=SIGNATURE-SECRET;COOKIE=COOKIE-SECRET"
        "#token=FRAGMENT-SECRET"
    )

    sanitized = sanitize_public_source_url(raw_url)

    assert sanitized == (
        "https://example.com/private/path;%74oken=redacted;safe=matrix-value"
        "?safe=value&%61PI%5FKey=redacted"
        "&X-Amz-Signature=redacted;COOKIE=redacted"
    )
    for secret in (
        "url-user",
        "URL-PASSWORD",
        "MATRIX-SECRET",
        "QUERY-SECRET",
        "SIGNATURE-SECRET",
        "COOKIE-SECRET",
        "FRAGMENT-SECRET",
    ):
        assert secret not in sanitized


@pytest.mark.parametrize(
    "unsafe_url",
    [
        "pkv://entries/7",
        "mailto:user@example.com",
        "https://example.com:invalid/path",
        "https://example.com/path\n?token=secret",
    ],
)
def test_public_source_url_rejects_non_http_or_ambiguous_values(
    unsafe_url: str,
) -> None:
    assert sanitize_public_source_url(unsafe_url) == ""


def test_public_source_url_falls_back_to_origin_for_ambiguous_encoded_matrix() -> None:
    secret = "DOUBLE-ENCODED-MATRIX-SECRET"
    raw_url = (
        "https://example.com/private%3BTo%254Ben%3D"
        f"{secret}?safe=visible"
    )

    sanitized = sanitize_public_source_url(raw_url)

    assert sanitized == "https://example.com/?safe=visible"
    assert secret not in sanitized


@pytest.mark.parametrize(
    "raw_url",
    [
        (
            "https://example.com/p;%74oken%3DENCODED-MATRIX"
            "?q=ok&api_key%3DENCODED-QUERY#x"
        ),
        (
            "https://example.com/p;%2574oken%253DDOUBLE-MATRIX"
            "?q=ok&%2561pi_key%253DDOUBLE-QUERY#x"
        ),
    ],
)
def test_public_source_url_drops_encoded_separator_credentials(raw_url: str) -> None:
    sanitized = sanitize_public_source_url(raw_url)

    assert sanitized == "https://example.com/?q=ok"
    for secret in (
        "ENCODED-MATRIX",
        "ENCODED-QUERY",
        "DOUBLE-MATRIX",
        "DOUBLE-QUERY",
    ):
        assert secret not in sanitized


def test_public_source_url_drops_query_with_nested_encoded_delimiters() -> None:
    secret = "NESTED-DELIMITER-SECRET"
    raw_url = (
        "https://example.com/private?safe=visible%2526"
        f"To%254Ben%253D{secret}"
    )

    sanitized = sanitize_public_source_url(raw_url)

    assert sanitized == "https://example.com/private"
    assert secret not in sanitized


@pytest.mark.parametrize(
    "encoded_fragment",
    [
        "safe=visible%23token=FRAGMENT-SECRET",
        "safe=visible%2523token%253DFRAGMENT-SECRET",
    ],
)
def test_public_source_url_drops_encoded_fragment_credentials(
    encoded_fragment: str,
) -> None:
    sanitized = sanitize_public_source_url(
        f"https://example.com/a?{encoded_fragment}"
    )

    assert sanitized == "https://example.com/a"
    assert "FRAGMENT-SECRET" not in sanitized


@pytest.mark.parametrize(
    "raw_url, expected",
    [
        (
            "https://outer.example/r?next=https%3A%2F%2F"
            "NESTED-USER%3ANESTED-PASSWORD%40inner.example%2Fa",
            "https://outer.example/r",
        ),
        (
            "https://outer.example/r?next=https%253A%252F%252F"
            "NESTED-USER%253ANESTED-PASSWORD%2540inner.example%252Fa",
            "https://outer.example/r",
        ),
        (
            "https://outer.example/r;next=https%3A%2F%2F"
            "NESTED-USER%3ANESTED-PASSWORD%40inner.example%2Fa",
            "https://outer.example/",
        ),
        (
            "https://outer.example/r?next=https%3A"
            "NESTED-USER%3ANESTED-PASSWORD%40inner.example%2Fa",
            "https://outer.example/r",
        ),
        (
            "https://outer.example/r?next=https%253A%252F"
            "NESTED-USER%253ANESTED-PASSWORD%2540inner.example%252Fa",
            "https://outer.example/r",
        ),
        (
            "https://outer.example/r;next=http%3A"
            "NESTED-USER%3ANESTED-PASSWORD%40inner.example%2Fa",
            "https://outer.example/",
        ),
        (
            "https://outer.example/r?next=ftp%3A"
            "NESTED-USER%3ANESTED-PASSWORD%40inner.example%2Fa",
            "https://outer.example/r",
        ),
    ],
)
def test_public_source_url_drops_nested_encoded_url_userinfo(
    raw_url: str,
    expected: str,
) -> None:
    sanitized = sanitize_public_source_url(raw_url)

    assert sanitized == expected
    assert "NESTED-USER" not in sanitized
    assert "NESTED-PASSWORD" not in sanitized


@pytest.mark.parametrize(
    "raw_url, expected",
    [
        (
            "https://outer.example/r?next=https%3A%5C%5C"
            "NESTED-USER%3ANESTED-PASSWORD%40inner.example%5Ca",
            "https://outer.example/r",
        ),
        (
            "https://outer.example/r?next=https%253A%255C%255C"
            "NESTED-USER%253ANESTED-PASSWORD%2540inner.example%255Ca",
            "https://outer.example/r",
        ),
        (
            "https://outer.example/r;next=https%3A%5C%5C"
            "NESTED-USER%3ANESTED-PASSWORD%40inner.example%5Ca",
            "https://outer.example/",
        ),
        (
            "https://outer.example/r?note=safe%0D%0A"
            "Authorization%3A%20Bearer%20CONTROL-SECRET",
            "https://outer.example/r",
        ),
        (
            "https://outer.example/r;note=safe%0D%0A"
            "Authorization=Bearer-CONTROL-SECRET",
            "https://outer.example/",
        ),
    ],
)
def test_public_source_url_drops_encoded_transport_hazards(
    raw_url: str,
    expected: str,
) -> None:
    sanitized = sanitize_public_source_url(raw_url)

    assert sanitized == expected
    assert "NESTED-USER" not in sanitized
    assert "NESTED-PASSWORD" not in sanitized
    assert "CONTROL-SECRET" not in sanitized


@pytest.mark.parametrize(
    "raw_url, expected",
    [
        (
            "https://outer.example/r?next=file%3A%2F%2F%2FC%3A%2FUsers%2F"
            "alice%2FLOCAL-PATH-SECRET.md",
            "https://outer.example/r",
        ),
        (
            "https://outer.example/r?next=%252Fhome%252Falice%252F"
            "LOCAL-PATH-SECRET.md",
            "https://outer.example/r",
        ),
        (
            "https://outer.example/r;next=file%3A%2F%2F%2FC%3A%2FUsers%2F"
            "alice%2FLOCAL-PATH-SECRET.md",
            "https://outer.example/",
        ),
        (
            "https://outer.example/r;next=%2Fhome%2Falice%2F"
            "LOCAL-PATH-SECRET.md",
            "https://outer.example/",
        ),
    ],
)
def test_public_source_url_drops_nested_encoded_local_references(
    raw_url: str,
    expected: str,
) -> None:
    sanitized = sanitize_public_source_url(raw_url)

    assert sanitized == expected
    assert "LOCAL-PATH-SECRET" not in sanitized


def test_nested_evidence_uses_shared_public_url_sanitizer() -> None:
    secret = "NESTED-URL-SECRET"
    sanitized = sanitize_public_evidence(
        {
            "knowledge_id": 7,
            "source_url": f"https://user:password@example.com/a?To%4Ben={secret}",
            "citation_source": (
                f"https://citation-user:citation-pass@example.com/a?AUTH={secret}"
            ),
        }
    )

    assert sanitized["source_url"] == "https://example.com/a?To%4Ben=redacted"
    assert sanitized["citation_source"] == "https://example.com/a?AUTH=redacted"
    assert secret not in repr(sanitized)
    assert "password" not in repr(sanitized)
    assert "citation-pass" not in repr(sanitized)


def test_nested_evidence_preserves_only_canonical_entry_source_locator() -> None:
    assert sanitize_public_evidence(
        {"knowledge_id": 7, "citation_source": "pkv://entries/7"}
    )["citation_source"] == "pkv://entries/7"
    assert sanitize_public_evidence(
        {
            "knowledge_id": 7,
            "citation_source": "pkv://user:secret@entries/7?token=secret",
        }
    )["citation_source"] == "pkv://entries/7"


@pytest.mark.parametrize(
    "malformed_source",
    [
        "ht!tps://url-user:URL-PASSWORD@example.com/a?token=QUERY-SECRET",
        "www.example.com/a?token=QUERY-SECRET",
        "://url-user:URL-PASSWORD@example.com/a?token=QUERY-SECRET",
        "%2568t!tps%253A%252F%252Fexample.com%252Fa%253Ftoken%253DQUERY-SECRET",
        "token=QUERY-SECRET",
    ],
)
def test_nested_evidence_fails_closed_for_malformed_url_like_source(
    malformed_source: str,
) -> None:
    sanitized = sanitize_public_evidence(
        {"knowledge_id": 7, "citation_source": malformed_source}
    )

    assert sanitized["citation_source"] == "pkv://entries/7"
    assert "URL-PASSWORD" not in repr(sanitized)
    assert "QUERY-SECRET" not in repr(sanitized)


def test_nested_evidence_redacts_double_encoded_local_paths_everywhere() -> None:
    encoded_path = "%252Fhome%252Falice%252Fsecret.md"
    sanitized = sanitize_public_evidence(
        {
            "knowledge_id": 7,
            "source_url": encoded_path,
            "source": encoded_path,
            "citation_source": encoded_path,
            "nested": encoded_path,
            encoded_path: "dynamic-key",
        }
    )

    assert sanitized["source_url"] == ""
    assert sanitized["source"] == "pkv://entries/7"
    assert sanitized["citation_source"] == "pkv://entries/7"
    assert sanitized["nested"] == "[redacted-local-reference]"
    assert sanitized["[redacted-local-reference]"] == "dynamic-key"
    assert "/home/alice" not in repr(sanitized)


def _encode_layers(value: str, count: int) -> str:
    encoded = value
    for _ in range(count):
        encoded = quote(encoded, safe="")
    return encoded


def test_canonicalization_budget_exhaustion_fails_closed() -> None:
    deep_query = _encode_layers("&token=DEEP-QUERY-SECRET", 9)
    deep_matrix = _encode_layers(";token=DEEP-MATRIX-SECRET", 9)
    deep_path = _encode_layers("/home/alice/DEEP-PATH-SECRET.md", 9)

    query_url = sanitize_public_source_url(
        f"https://example.com/a?safe=visible{deep_query}"
    )
    matrix_url = sanitize_public_source_url(f"https://example.com/a{deep_matrix}")
    evidence = sanitize_public_evidence(
        {
            "knowledge_id": 7,
            "source": deep_path,
            "citation_source": deep_path,
            "nested": deep_path,
            deep_path: "dynamic-key",
        }
    )

    assert query_url == ""
    assert matrix_url == ""
    assert evidence["source"] == "pkv://entries/7"
    assert evidence["citation_source"] == "pkv://entries/7"
    assert evidence["nested"] == "[redacted-local-reference]"
    assert evidence["[redacted-local-reference]"] == "dynamic-key"
    combined = repr((query_url, matrix_url, evidence))
    for secret in (
        "DEEP-QUERY-SECRET",
        "DEEP-MATRIX-SECRET",
        "DEEP-PATH-SECRET",
    ):
        assert secret not in combined


@pytest.mark.parametrize(
    "malformed_url",
    [
        "https://[invalid/path?token=BRACKET-SECRET",
        "http://[::1",
        "file://[invalid/path",
    ],
)
def test_malformed_bracket_authority_fails_closed_without_raising(
    malformed_url: str,
) -> None:
    assert is_local_reference(malformed_url) is True
    assert sanitize_public_source_url(malformed_url) == ""
    sanitized = sanitize_public_evidence(
        {"knowledge_id": 7, "citation_source": malformed_url}
    )
    assert sanitized["citation_source"] == "pkv://entries/7"
    assert "BRACKET-SECRET" not in repr(sanitized)


@pytest.mark.parametrize(
    "malformed_shape",
    [
        ["https://url-user:URL-PASSWORD@example.com/a?token=QUERY-SECRET"],
        {"url": "https://url-user:URL-PASSWORD@example.com/a?token=QUERY-SECRET"},
        ("https://url-user:URL-PASSWORD@example.com/a?token=QUERY-SECRET",),
    ],
)
def test_source_fields_reject_non_string_shapes(malformed_shape: object) -> None:
    sanitized = sanitize_public_evidence(
        {
            "knowledge_id": 7,
            "source": malformed_shape,
            "citation_source": malformed_shape,
        }
    )

    assert sanitized["source"] == "pkv://entries/7"
    assert sanitized["citation_source"] == "pkv://entries/7"
    assert "URL-PASSWORD" not in repr(sanitized)
    assert "QUERY-SECRET" not in repr(sanitized)
