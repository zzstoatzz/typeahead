import { handleSearch } from "../src/handlers/search.ts";

export default {
  fetch(request, env, ctx) {
    return handleSearch(request, env.DB, env, ctx);
  },
};
