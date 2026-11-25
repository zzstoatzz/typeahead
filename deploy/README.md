# Deployment tooling

The public API, search transport, image renderer and eval collector are
configured in [cloudflare.config.ts](../cloudflare.config.ts). Both store
slots use the [slot image](slot/README.md).
Use the root `justfile` deploy commands for the Workers so lint and the normal
verification gates run before publication. Slots are deployed and the route is
changed by the procedures in [operating](../docs/operating.md).
