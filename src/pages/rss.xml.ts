import rss from '@astrojs/rss';
import type { APIContext } from 'astro';
import { getCollection } from 'astro:content';
import { site } from '../data/site';

export async function GET(context: APIContext) {
  const labs = (await getCollection('lab')).sort(
    (a, b) => b.data.date.getTime() - a.data.date.getTime()
  );

  return rss({
    title: `${site.name} · 实验日志`,
    description: '嵌入式系统课程 8 个实验的完整记录，以及在 i.MX6ULL 上部署静态站点的工程实践。',
    site: context.site ?? 'https://imx6ull.example.com',
    items: labs.map((lab) => ({
      title: lab.data.title,
      description: lab.data.summary,
      pubDate: lab.data.date,
      link: `/lab/${lab.id}/`,
      categories: lab.data.tags
    })),
    customData: '<language>zh-CN</language>',
    stylesheet: false
  });
}
