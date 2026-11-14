# Deployment tooling

Production uses the root Worker configuration, the
[search transport configuration](search-transport/wrangler.jsonc), the
[image renderer configuration](image-renderer/wrangler.jsonc) and the
[slot image](slot/README.md) both store slots run.
Use the root `justfile` deploy commands for the Workers so lint and the normal
verification gates run before publication. Slots are deployed and the route is
changed by the procedures in [operating](../docs/operating.md).
