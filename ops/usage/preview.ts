import { costsPage } from '../../src/pages/costs';
await Bun.write(new URL('../../docs/drafts/usage/index.html', import.meta.url), costsPage());
