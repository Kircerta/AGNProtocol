# Configuration

AGNProtocol reads provider and integration settings from environment variables
and portable config files.

## Provider Variables

```bash
DEEPSEEK_API_KEY
QWEN_LOCAL_BASE_URL
QWEN_LOCAL_MODEL
TELEGRAM_BOT_TOKEN
ALLOWED_CHAT_IDS
JWT_SECRET
```

## Local Files

These paths are ignored by git:

- `.local/`
- `.env`
- `HOST_INFO.md`
- `agn2/admin_profile.json`
- `config/tool_reality_cards.json`
- `runtime/`
- `reports/`
- `results/`
- `verdicts/`
- `.agn_workspace/`

Use `agn2/admin_profile.example.json` and
`config/tool_reality_cards.example.json` as templates.
