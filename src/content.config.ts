import { defineCollection, z } from 'astro:content';
import { glob } from 'astro/loaders';

// 实验日志：src/content/lab/*.md
const lab = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/lab' }),
  schema: z.object({
    title: z.string(),
    order: z.number(),
    week: z.string(), // 第几次
    date: z.coerce.date(),
    summary: z.string(),
    tags: z.array(z.string()).default([]),
    duration: z.string().default('4 学时'),
    hardware: z.array(z.string()).default([]),
    status: z.enum(['done', 'doing', 'waiting']).default('done')
  })
});

// 技术选型 / 工程笔记：src/content/note/*.md
const note = defineCollection({
  loader: glob({ pattern: '**/*.md', base: './src/content/note' }),
  schema: z.object({
    title: z.string(),
    date: z.coerce.date(),
    summary: z.string(),
    tags: z.array(z.string()).default([]),
    order: z.number().default(99),
    reading: z.string().default('6 分钟')
  })
});

export const collections = { lab, note };
