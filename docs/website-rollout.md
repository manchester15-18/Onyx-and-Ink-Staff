# Onyx & Ink website rollout

Cloudflare Pages project: `onyx-and-ink-site`
Preview address: `https://onyx-and-ink-site.pages.dev`

1. Create or open the Onyx & Ink project in Webstudio.
2. Build and review it on Webstudio's protected staging address.
3. Export the approved static site into `website/dist/`.
4. Deploy `website/dist/` to the Cloudflare Pages project as a preview.
5. Test navigation, mobile layout, accessibility, Square checkout links, and SEO metadata.
6. Only after approval, connect the production domain in Cloudflare and switch DNS away from Squarespace.

Never put Square access tokens, payment keys, or dashboard credentials in exported site files. Checkout uses Square-hosted payment links or a server-side Cloudflare Worker.
