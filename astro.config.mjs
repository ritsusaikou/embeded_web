import { defineConfig } from 'astro/config';
import sitemap from '@astrojs/sitemap';

// https://astro.build/config
export default defineConfig({
  // 部署前改成你自己的域名（用于 RSS / sitemap / canonical）
  site: 'https://blog.example.com',
  output: 'static',
  trailingSlash: 'never',
  compressHTML: true,
  integrations: [sitemap()],
  build: {
    inlineStylesheets: 'auto'
  }
});
