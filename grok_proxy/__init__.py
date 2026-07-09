"""grok_proxy — stdlib-only Anthropic Messages <-> xAI Responses translation proxy.

Bundled with the sublime-claude plugin. Exposes an Anthropic-compatible
surface (POST /v1/messages, /v1/messages/count_tokens, GET /v1/models) and
translates to xAI's Responses API, authenticating via xAI SuperGrok OAuth2+PKCE.

Runnable standalone:
    python -m grok_proxy --port 8787 --auth-token <token> [--login]
"""
