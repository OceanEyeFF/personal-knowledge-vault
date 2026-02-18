"""
MCP 安全验证单元测试

测试 URL 验证、内网 IP 检测、文本长度限制和 HTTP Bearer Token 认证。
"""

import sys
import os
from pathlib import Path
from unittest.mock import patch

import pytest

# 确保项目根目录在 Python path 中
PROJECT_ROOT = Path(__file__).parent.parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

from src.mcp.utils import (
    validate_url,
    is_private_ip,
    validate_url_security,
    validate_text_length,
    validate_http_auth,
)


# ============================================================
# validate_url 测试
# ============================================================

class TestValidateUrl:
    """URL 格式验证测试。"""

    def test_valid_http_url(self):
        valid, error = validate_url("http://example.com")
        assert valid is True
        assert error == ""

    def test_valid_https_url(self):
        valid, error = validate_url("https://example.com/path?q=test")
        assert valid is True
        assert error == ""

    def test_empty_url(self):
        valid, error = validate_url("")
        assert valid is False
        assert "不能为空" in error

    def test_none_url(self):
        valid, error = validate_url(None)
        assert valid is False

    def test_whitespace_url(self):
        valid, error = validate_url("   ")
        assert valid is False

    def test_no_scheme(self):
        valid, error = validate_url("example.com")
        assert valid is False
        assert "scheme" in error.lower() or "http" in error

    def test_ftp_scheme_rejected(self):
        valid, error = validate_url("ftp://example.com")
        assert valid is False
        assert "http" in error

    def test_javascript_scheme_rejected(self):
        valid, error = validate_url("javascript:alert(1)")
        assert valid is False

    def test_no_netloc(self):
        valid, error = validate_url("http://")
        assert valid is False
        assert "域名" in error or "IP" in error


# ============================================================
# is_private_ip 测试
# ============================================================

class TestIsPrivateIp:
    """内网 IP 检测测试。"""

    def test_localhost(self):
        assert is_private_ip("localhost") is True

    def test_loopback_ipv4(self):
        assert is_private_ip("127.0.0.1") is True

    def test_loopback_ipv4_other(self):
        assert is_private_ip("127.0.0.2") is True

    def test_class_a_private(self):
        assert is_private_ip("10.0.0.1") is True
        assert is_private_ip("10.255.255.255") is True

    def test_class_b_private(self):
        assert is_private_ip("172.16.0.1") is True
        assert is_private_ip("172.31.255.255") is True

    def test_class_c_private(self):
        assert is_private_ip("192.168.0.1") is True
        assert is_private_ip("192.168.1.100") is True

    def test_ipv6_loopback(self):
        assert is_private_ip("::1") is True

    def test_public_ip(self):
        assert is_private_ip("8.8.8.8") is False
        assert is_private_ip("1.1.1.1") is False

    def test_public_domain(self):
        assert is_private_ip("example.com") is False
        assert is_private_ip("mp.weixin.qq.com") is False

    def test_local_domain(self):
        assert is_private_ip("myhost.local") is True

    def test_internal_domain(self):
        assert is_private_ip("server.internal") is True

    def test_empty_hostname(self):
        assert is_private_ip("") is True

    def test_172_15_not_private(self):
        """172.15.x.x 不在 172.16-31 范围内，应为公网。"""
        assert is_private_ip("172.15.0.1") is False

    def test_172_32_not_private(self):
        """172.32.x.x 不在 172.16-31 范围内，应为公网。"""
        assert is_private_ip("172.32.0.1") is False


# ============================================================
# validate_url_security 综合测试
# ============================================================

class TestValidateUrlSecurity:
    """URL 综合安全验证测试。"""

    def test_valid_public_url(self):
        valid, error = validate_url_security("https://mp.weixin.qq.com/article")
        assert valid is True

    def test_reject_localhost(self):
        valid, error = validate_url_security("http://localhost/admin")
        assert valid is False
        assert "内网" in error

    def test_reject_127(self):
        valid, error = validate_url_security("http://127.0.0.1:8080/secret")
        assert valid is False
        assert "内网" in error

    def test_reject_10_network(self):
        valid, error = validate_url_security("http://10.0.0.1/api")
        assert valid is False

    def test_reject_192_168(self):
        valid, error = validate_url_security("http://192.168.1.1/admin")
        assert valid is False

    def test_reject_172_16(self):
        valid, error = validate_url_security("http://172.16.0.1/internal")
        assert valid is False

    def test_invalid_url_format(self):
        valid, error = validate_url_security("not-a-url")
        assert valid is False

    def test_whitespace_trimmed(self):
        valid, error = validate_url_security("  https://example.com  ")
        assert valid is True


# ============================================================
# validate_text_length 测试
# ============================================================

class TestValidateTextLength:
    """文本长度验证测试。"""

    def test_normal_text(self):
        valid, error = validate_text_length("这是一段正常的文本")
        assert valid is True

    def test_empty_text(self):
        valid, error = validate_text_length("")
        assert valid is False
        assert "不能为空" in error

    def test_whitespace_only(self):
        valid, error = validate_text_length("   ")
        assert valid is False

    def test_max_length_exact(self):
        text = "A" * 100000
        valid, error = validate_text_length(text)
        assert valid is True

    def test_exceeds_max_length(self):
        text = "A" * 100001
        valid, error = validate_text_length(text)
        assert valid is False
        assert "超过限制" in error

    def test_custom_max_length(self):
        text = "A" * 100
        valid, error = validate_text_length(text, max_length=50)
        assert valid is False

    def test_custom_max_length_ok(self):
        text = "A" * 30
        valid, error = validate_text_length(text, max_length=50)
        assert valid is True


# ============================================================
# validate_http_auth 测试
# ============================================================

class TestValidateHttpAuth:
    """HTTP Bearer Token 认证测试。"""

    @patch("src.mcp.utils._MCP_AUTH_TOKEN", "test-secret-token")
    def test_valid_token(self):
        headers = {"Authorization": "Bearer test-secret-token"}
        assert validate_http_auth(headers) is True

    @patch("src.mcp.utils._MCP_AUTH_TOKEN", "test-secret-token")
    def test_invalid_token(self):
        headers = {"Authorization": "Bearer wrong-token"}
        assert validate_http_auth(headers) is False

    @patch("src.mcp.utils._MCP_AUTH_TOKEN", "test-secret-token")
    def test_missing_auth_header(self):
        headers = {}
        assert validate_http_auth(headers) is False

    @patch("src.mcp.utils._MCP_AUTH_TOKEN", "test-secret-token")
    def test_non_bearer_auth(self):
        headers = {"Authorization": "Basic dXNlcjpwYXNz"}
        assert validate_http_auth(headers) is False

    @patch("src.mcp.utils._MCP_AUTH_TOKEN", "")
    def test_no_token_configured_rejects_all(self):
        """未配置 Token 时，拒绝所有 HTTP 请求（安全默认）。"""
        headers = {"Authorization": "Bearer anything"}
        assert validate_http_auth(headers) is False

    @patch("src.mcp.utils._MCP_AUTH_TOKEN", None)
    def test_none_token_rejects_all(self):
        headers = {"Authorization": "Bearer anything"}
        assert validate_http_auth(headers) is False
