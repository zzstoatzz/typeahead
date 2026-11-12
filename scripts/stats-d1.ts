import { main } from "../src/stats-migration/cli";

process.exit(await main(process.argv.slice(2), {
  fetch: (url, init) => fetch(url, init),
  print: (line) => console.log(line),
  secret: process.env.TYPEAHEAD_SECRET,
  now: () => Date.now(),
}));
