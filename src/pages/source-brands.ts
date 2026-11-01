/** Icons and sites for traffic sources, kept apart from the page logic.
 *
 *  A source is recorded under its X-Client name, Origin or Referer host. A
 *  host gets `https://<host>/favicon.ico` without an entry here. Everything
 *  else needs one: a client name (`skytrace`) is not a website, and some
 *  sites serve no usable favicon. Add an entry only after checking that the
 *  site belongs to the app and that the icon URL serves an image.
 */

export type SourceBrand = {
  /** where "Visit website" goes; omitted when the source name is already the host */
  site?: string;
  icon: string;
};

const brandIcon = (namespace: string) =>
  `https://brand.waow.tech/xrpc/tech.waow.brand.getIcon?namespace=${encodeURIComponent(namespace)}`;

export const SOURCE_BRANDS = {
  // client names, checked 2026-11-01 against the app's own site and Bluesky account
  skytrace: { site: "https://skytrace.aly.town", icon: "https://skytrace.aly.town/icons/apple-touch-icon.png" },
  pckt: { site: "https://pckt.blog", icon: "https://pckt.blog/apple-touch-icon.png" },
  happyview: { site: "https://happyview.dev", icon: "https://happyview.dev/img/favicon.png" },
  "voxport-studio": { site: "https://voxport.live", icon: "https://voxport.live/icon1.png" },
  lazurite: { site: "https://lazurite.stormlightlabs.org", icon: "https://lazurite.stormlightlabs.org/static/favicon.svg" },
  "northsky-pronouns": { site: "https://northsky.app", icon: "https://northsky.app/static/apple-touch-icon.png" },
  homepage: { site: "https://typeahead.waow.tech", icon: "https://typeahead.waow.tech/apple-touch-icon.png" },

  // hosts whose favicon.ico is missing or poor
  "northsky.app": { icon: "https://northsky.app/static/apple-touch-icon.png" },
  "plyr.fm": { icon: brandIcon("fm.plyr") },
  "bsky38.com": { icon: brandIcon("com.bsky38") },
  "marque.at": { icon: brandIcon("at.marque") },
  "standard-reader.app": { icon: brandIcon("app.standard-reader") },
  "userinput.app": { icon: brandIcon("app.userinput") },
  "youandme.at": { icon: brandIcon("at.youandme") },
} satisfies Record<string, SourceBrand>;
