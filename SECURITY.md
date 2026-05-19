# Security Policy

AGNProtocol is a local agent runtime. Treat provider keys, Telegram tokens,
browser profiles, runtime directories, screenshots, and local archives as
local operator state.

## Do Not Commit

- `.env` files or shell history
- API keys, provider tokens, Telegram tokens, webhook secrets, or JWT secrets
- runtime state under `runtime/`, `reports/`, `dispatch/`, `results/`,
  `verdicts/`, `.agn_workspace/`, or `memory/records/`
- local model paths, screenshots, archives, and host-specific notes

## Configuration

Use environment variables or a local secret manager for credentials:

```bash
DEEPSEEK_API_KEY
QWEN_LOCAL_BASE_URL
QWEN_LOCAL_MODEL
TELEGRAM_BOT_TOKEN
ALLOWED_CHAT_IDS
JWT_SECRET
```

## Vulnerability Reports

Do not include secrets or exploit details in public issues. Use GitHub
vulnerability reporting when available, or open a minimal issue requesting a
confidential disclosure channel.

## Local Scan

```bash
python3 scripts/maintenance/check_portability.py
git diff --check
rg --hidden --glob '!.git/**' -n -I 'sk-[A-Za-z0-9]{20,}|gh[pousr]_[A-Za-z0-9_]{20,}|AIza[0-9A-Za-z_-]{20,}|BEGIN (RSA|OPENSSH|PRIVATE) KEY' .
```
