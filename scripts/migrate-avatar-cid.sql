-- one-shot migration: convert full avatar URLs to CIDs
-- strips the URL prefix (including DID) and @jpeg suffix, leaving just the CID
--
-- run with: npx wrangler d1 execute typeahead --remote --file scripts/migrate-avatar-cid.sql

UPDATE actors
SET avatar_url = REPLACE(
  REPLACE(avatar_url, 'https://cdn.bsky.app/img/avatar/plain/' || did || '/', ''),
  '@jpeg',
  ''
)
WHERE avatar_url LIKE 'https://cdn.bsky.app/%';
