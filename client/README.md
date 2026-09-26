# DOVideo Python R1 Web Client

This Vue 3/Vite client is adapted from the public DOVideo-AI client. See
`NOTICE.md` for provenance and the upstream MIT-license requirement.

```bash
npm ci
npm run dev
```

The development proxy targets `http://127.0.0.1:8000` by default. Set
`VITE_DEV_PROXY_TARGET` in `.env` for another backend, or set
`VITE_API_BASE_URL` when deploying the frontend separately.

The isolated UI-0 visual system is available at `/design-lab` while Vite runs.
It uses illustrative data and makes no backend requests. See
`../docs/design/DESIGN_SYSTEM.md` for tokens, components, and contribution rules.
