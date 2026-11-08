import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

// https://astro.build/config
export default defineConfig({
  // 用于 canonical / RSS / sitemap
  site: 'https://board.nexuslink.icu',
  output: 'static',
  trailingSlash: 'always',
  compressHTML: true,
  integrations: [sitemap()],
  build: {
    // 单页 CSS 超过 4KB，保持外链以便跨页缓存
    inlineStylesheets: 'auto'
  }
});
