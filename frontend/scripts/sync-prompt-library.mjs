#!/usr/bin/env node

import { createHash } from "node:crypto";
import { execFileSync } from "node:child_process";
import { existsSync } from "node:fs";
import { mkdir, readFile, writeFile } from "node:fs/promises";
import path from "node:path";
import { fileURLToPath } from "node:url";
import sharp from "sharp";

const SCRIPT_DIR = path.dirname(fileURLToPath(import.meta.url));
const FRONTEND_DIR = path.resolve(SCRIPT_DIR, "..");
const LIBRARY_DIR = path.join(FRONTEND_DIR, "public", "prompt-library");
const THUMBS_DIR = path.join(LIBRARY_DIR, "thumbs");
const DATA_PATH = path.join(LIBRARY_DIR, "prompt-data.json");
const PREVIEW_PATH = path.join(LIBRARY_DIR, "prompt-preview.json");
const LICENSE_PATH = path.join(LIBRARY_DIR, "license.json");

const SOURCE_CONFIG = {
  evolink: {
    name: "EvoLinkAI/awesome-gpt-image-2-API-and-Prompts",
    url: "https://github.com/EvoLinkAI/awesome-gpt-image-2-API-and-Prompts",
    license: "CC0-1.0",
    licenseUrl: "https://creativecommons.org/publicdomain/zero/1.0/",
  },
  freestylefly: {
    name: "freestylefly/awesome-gpt-image-2",
    url: "https://github.com/freestylefly/awesome-gpt-image-2",
    license: "MIT",
    licenseUrl: "https://opensource.org/license/mit",
  },
  youmind: {
    name: "YouMind-OpenLab/awesome-gpt-image-2",
    url: "https://github.com/YouMind-OpenLab/awesome-gpt-image-2",
    license: "CC-BY-4.0",
    licenseUrl: "https://creativecommons.org/licenses/by/4.0/",
  },
};

const EVO_CATEGORIES = [
  "ad-creative",
  "character",
  "comparison",
  "ecommerce",
  "portrait",
  "poster",
  "ui",
];

const BLOCKED_PATTERNS = [
  ["adult-content", /\b(?:nsfw|nude|nudity|naked|porn|pornographic|erotic|fetish|sexualized|sexy|lingerie|underwear|cleavage|bikini|swimsuit|provocative|sultry)\b|deep\s+v[- ]?neck|high[- ]slit/i],
  ["public-figure", /\b(?:cristiano\s+ronaldo|ronaldo|lionel\s+messi|sam\s+altman|elon\s+musk|donald\s+trump|trump|taylor\s+swift|steve\s+jobs|lebron\s+james|ali\s+khamenei|khamenei)\b|\u7f57\u7eb3\u5c14\u591a|\u6885\u897f|\u9a6c\u65af\u514b|\u7279\u6717\u666e|\u54c8\u6885\u5185\u4f0a/i],
  ["protected-brand-or-ip", /\b(?:adidas|nike|rapha|coca[- ]?cola|pepsi|starbucks|mcdonald['\u2019]?s|kfc|lego|fifa|world\s+cup|netflix|pixar|disney|ghibli|marvel|pokemon|doraemon|dragon\s+ball|one\s+piece|naruto|minecraft|harry\s+potter|ray[- ]?ban|glossier|kinder\s+joy|meta\s+quest|persona\s*5|gta\s*(?:6|vi)|league\s+of\s+legends|cowboy\s+bebop|spike\s+spiegel|totoro|youtube|sony|samsung|fujifilm|chanel|gucci|prada|louis\s+vuitton|bmw|psg|vogue|gq|douyin|tiktok|wechat|weixin|xiaohongshu)\b|\u82f1\u96c4\u8054\u76df|\u9f99\u732b|\u5c0f\u7ea2\u4e66|\u5fae\u4fe1|\u5fae\u535a|\bapple(?:[- ]style|\s+logo|\s+inc\.?|\s+keynote)\b/i],
  ["protected-product", /\b(?:iphone|macbook|playstation|nintendo\s+switch)\b/i],
];

function parseArgs(argv) {
  const values = new Map();
  const flags = new Set();
  for (let index = 0; index < argv.length; index += 1) {
    const token = argv[index];
    if (!token.startsWith("--")) continue;
    const next = argv[index + 1];
    if (next && !next.startsWith("--")) {
      values.set(token, next);
      index += 1;
    } else {
      flags.add(token);
    }
  }
  return { values, flags };
}

function requiredDirectory(args, name) {
  const value = args.values.get(`--${name}`);
  if (!value) throw new Error(`Missing --${name} <checkout>`);
  const resolved = path.resolve(value);
  if (!existsSync(resolved)) throw new Error(`Checkout does not exist: ${resolved}`);
  return resolved;
}

function compact(value) {
  return String(value || "").replace(/\s+/g, " ").trim();
}

function normalizedText(value) {
  return compact(value).toLowerCase();
}

function normalizedUrl(value) {
  try {
    const url = new URL(String(value || ""));
    url.hash = "";
    url.search = "";
    url.hostname = url.hostname.toLowerCase().replace(/^www\./, "");
    return url.toString().replace(/\/$/, "");
  } catch {
    return "";
  }
}

function shortHash(value) {
  return createHash("sha256").update(String(value)).digest("hex").slice(0, 12);
}

function gitRevision(repo) {
  return execFileSync("git", ["-C", repo, "rev-parse", "HEAD"], { encoding: "utf8" }).trim();
}

function extractPrompt(block) {
  const boldMatches = [...block.matchAll(/\*\*(?:Prompt|\u63d0\u793a\u8bcd)[^*\n]*\*\*\s*[:\uff1a]?\s*```[^\n]*\n([\s\S]*?)\n```/gi)];
  const headingMatches = [...block.matchAll(/####\s+[^\n]*(?:Prompt|\u63d0\u793a\u8bcd)[^\n]*\s*```[^\n]*\n([\s\S]*?)\n```/gi)];
  return [...boldMatches, ...headingMatches]
    .map((match) => match[1].trim())
    .filter(Boolean)
    .join("\n\n---\n\n");
}

function extractImageUrls(block) {
  return [...block.matchAll(/<img[^>]+src=["']([^"']+)["']/gi)]
    .map((match) => match[1].trim())
    .filter((value, index, values) => value && values.indexOf(value) === index);
}

function extractMarkdownLink(line) {
  const match = String(line || "").match(/\[([^\]]+)\]\((https?:\/\/[^)]+)\)/);
  return match ? { label: match[1].trim(), url: match[2].trim() } : null;
}

function tagsFor(candidate) {
  const text = normalizedText(`${candidate.title} ${candidate.prompt}`);
  const rules = [
    ["1:1", /\b1\s*:\s*1\b|\bsquare\b/],
    ["4:5", /\b4\s*:\s*5\b/],
    ["9:16", /\b9\s*:\s*16\b|\bvertical smartphone\b/],
    ["3D", /\b3d\b|three-dimensional/],
    ["UI", /\bui\b|interface|dashboard|app screen|website/],
    ["poster", /poster|flyer|typography/],
    ["portrait", /portrait|selfie|headshot/],
    ["photorealistic", /photoreal|photo-real|realistic photography/],
    ["cinematic", /cinematic|film still/],
    ["product", /product|e-commerce|ecommerce|packaging/],
    ["branding", /brand|logo|campaign/],
    ["illustration", /illustration|storybook|watercolor|ink drawing/],
    ["infographic", /infographic|diagram|chart/],
    ["storyboard", /storyboard|contact sheet|shot list/],
    ["vintage", /vintage|retro|nostalg/],
    ["minimal", /minimal|negative space/],
  ];
  const discovered = rules.filter(([, pattern]) => pattern.test(text)).map(([tag]) => tag);
  return [...new Set([...(candidate.tags || []), ...discovered])].slice(0, 8);
}

function riskReason(candidate) {
  const text = `${candidate.title}\n${candidate.prompt}`;
  if (compact(candidate.prompt).length < 40) return "incomplete-prompt";
  if (!candidate.imageSource) return "missing-preview";
  for (const [reason, pattern] of BLOCKED_PATTERNS) {
    if (pattern.test(text)) return reason;
  }
  return "";
}

function mapFreestyleCategory(candidate) {
  const category = candidate.category;
  const text = normalizedText(`${candidate.title} ${(candidate.styles || []).join(" ")} ${(candidate.scenes || []).join(" ")}`);
  if (category === "Products & E-commerce") return "ecommerce";
  if (category === "Brand & Logos") return "ad-creative";
  if (category === "Characters & People") return /portrait|photo|realistic/.test(text) ? "portrait" : "character";
  if (category === "Photography & Realism") return /product|food|commerce/.test(text) ? "ecommerce" : "portrait";
  if (category === "UI & Interfaces" || category === "Charts & Infographics" || category === "Documents & Publishing") return "ui";
  if (/product|commerce|packaging|advertis/.test(text)) return "ecommerce";
  if (/portrait|selfie|fashion|photo/.test(text)) return "portrait";
  if (/character|mascot/.test(text)) return "character";
  return "poster";
}

function mapYouMindCategory(title) {
  const text = normalizedText(title);
  if (/e-commerce|ecommerce|product marketing|product photo|food|drink|packaging/.test(text)) return "ecommerce";
  if (/app \/ web|ui|interface|dashboard|website|infographic|edu visual|diagram|chart/.test(text)) return "ui";
  if (/profile \/ avatar|portrait|selfie|photography|fashion editorial/.test(text)) return "portrait";
  if (/character|game asset|mascot/.test(text)) return "character";
  if (/comparison|before|after|replacement|transformation|style transfer|storyboard/.test(text)) return "comparison";
  if (/social media|youtube thumbnail|advertisement|campaign|brand/.test(text)) return "ad-creative";
  return "poster";
}

function resolveEvoImage(repo, source) {
  if (!source) return "";
  if (/^https?:\/\//i.test(source)) {
    const match = source.match(/githubusercontent\.com\/[^/]+\/[^/]+\/(?:refs\/heads\/)?main\/(.+)$/i);
    return match ? path.join(repo, decodeURIComponent(match[1])) : source;
  }
  return path.resolve(repo, "cases", source);
}

function evoCaseId(images, fallbackCaseId) {
  const match = String(images[0] || "").match(/_case(\d+)\//i);
  return Number(match?.[1] || fallbackCaseId);
}

function parseEvoHeadings(markdown, category, repo) {
  const headings = [...markdown.matchAll(/^### Case (\d+)(?::\s*(.*))?$/gm)];
  const imageMap = new Map();
  for (let index = 0; index < headings.length; index += 1) {
    const match = headings[index];
    const block = markdown.slice(match.index, headings[index + 1]?.index ?? markdown.length);
    const images = extractImageUrls(block);
    const caseId = evoCaseId(images, match[1]);
    if (images.length) imageMap.set(`${category}:${caseId}`, resolveEvoImage(repo, images[0]));
  }
  return imageMap;
}

function parseEvoCandidates(markdown, category, repo) {
  const headings = [...markdown.matchAll(/^### Case (\d+)(?::\s*(.*))?$/gm)];
  const candidates = [];
  for (let index = 0; index < headings.length; index += 1) {
    const match = headings[index];
    const heading = match[2] || "";
    const block = markdown.slice(match.index, headings[index + 1]?.index ?? markdown.length);
    const titleLink = extractMarkdownLink(heading);
    const sourceLine = block.match(/^\*\*Source[:\uff1a]?\*\*\s*[:\uff1a]?\s*(.+)$/mi)?.[1] || "";
    const sourceLink = extractMarkdownLink(sourceLine);
    const author = heading.match(/\(by\s+\[([^\]]+)]\([^)]+\)\s*\)\s*$/i)?.[1]
      || heading.match(/\(by\s+([^)]+)\)\s*$/i)?.[1]
      || sourceLink?.label
      || "";
    const images = extractImageUrls(block);
    const caseId = evoCaseId(images, match[1]);
    const prompt = extractPrompt(block);
    candidates.push({
      id: `${category}-${caseId}`,
      category,
      caseId,
      title: compact(titleLink?.label || heading.replace(/\s*\(by\s+.+$/i, "") || `Case ${caseId}`),
      author: compact(author),
      sourceUrl: titleLink?.url || sourceLink?.url || "",
      prompt,
      imageCount: images.length,
      imageSource: resolveEvoImage(repo, images[0]),
      sourceRepository: SOURCE_CONFIG.evolink.name,
      sourceLicense: SOURCE_CONFIG.evolink.license,
    });
  }
  return candidates;
}

async function parseEvo(repo) {
  const candidates = [];
  const imageMap = new Map();
  for (const category of EVO_CATEGORIES) {
    const markdown = await readFile(path.join(repo, "cases", `${category}.md`), "utf8");
    for (const [key, value] of parseEvoHeadings(markdown, category, repo)) imageMap.set(key, value);
    candidates.push(...parseEvoCandidates(markdown, category, repo));
  }
  return { candidates, imageMap };
}

async function parseFreestylefly(repo) {
  const data = JSON.parse(await readFile(path.join(repo, "data", "cases.json"), "utf8"));
  return data.cases.map((entry) => {
    const category = mapFreestyleCategory(entry);
    const imagePath = String(entry.image || "").replace(/^\/images\//, "");
    return {
      id: `fs-${category}-${entry.id}`,
      category,
      caseId: Number(entry.id),
      title: compact(entry.title),
      author: compact(entry.sourceLabel),
      sourceUrl: entry.sourceUrl || entry.githubUrl || "",
      prompt: String(entry.prompt || "").trim(),
      imageCount: entry.image ? 1 : 0,
      imageSource: imagePath ? path.join(repo, "data", "images", imagePath) : "",
      tags: [...(entry.styles || []), ...(entry.scenes || [])],
      sourceRepository: SOURCE_CONFIG.freestylefly.name,
      sourceLicense: SOURCE_CONFIG.freestylefly.license,
    };
  });
}

async function parseYouMind(repo) {
  const markdown = await readFile(path.join(repo, "README.md"), "utf8");
  const blocks = markdown.split(/(?=^### No\. \d+: )/m).slice(1);
  return blocks.map((block) => {
    const heading = block.match(/^### No\. (\d+):\s*(.+)$/m);
    const title = compact(heading?.[2] || "Untitled prompt");
    const prompt = extractPrompt(block);
    const images = extractImageUrls(block).filter((url) => /cms-assets\.youmind\.com/i.test(url));
    const authorLine = block.match(/^- \*\*Author:\*\*\s*(.+)$/m)?.[1] || "";
    const sourceLine = block.match(/^- \*\*Source:\*\*\s*(.+)$/m)?.[1] || "";
    const author = extractMarkdownLink(authorLine)?.label || compact(authorLine);
    const sourceUrl = extractMarkdownLink(sourceLine)?.url || "";
    const remoteId = Number(block.match(/youmind\.com\/gpt-image-2-prompts\?id=(\d+)/)?.[1] || 0);
    const category = mapYouMindCategory(title);
    return {
      id: `ym-${category}-${remoteId || shortHash(`${title}\n${prompt}`)}`,
      category,
      caseId: Number(heading?.[1] || 0),
      sourceCaseId: remoteId || null,
      title,
      author: compact(author),
      sourceUrl,
      prompt,
      imageCount: images.length,
      imageSource: images[0] || "",
      sourceRepository: SOURCE_CONFIG.youmind.name,
      sourceLicense: SOURCE_CONFIG.youmind.license,
    };
  });
}

function selectCandidates(existingItems, sourceCandidates) {
  const itemIds = new Set(existingItems.map((item) => item.id));
  const promptKeys = new Set(existingItems.map((item) => normalizedText(item.prompt)).filter(Boolean));
  const identityKeys = new Set(existingItems.map((item) => `${normalizedUrl(item.sourceUrl)}|${normalizedText(item.title)}`));
  const accepted = [];
  const duplicate = [];
  const blocked = [];
  for (const candidate of sourceCandidates) {
    const promptKey = normalizedText(candidate.prompt);
    const identityKey = `${normalizedUrl(candidate.sourceUrl)}|${normalizedText(candidate.title)}`;
    const duplicateReason = itemIds.has(candidate.id)
      ? "id-collision"
      : promptKeys.has(promptKey)
        ? "same-prompt"
        : identityKeys.has(identityKey)
          ? "same-source-and-title"
          : "";
    if (duplicateReason) {
      duplicate.push({ id: candidate.id, title: candidate.title, source: candidate.sourceRepository, reason: duplicateReason });
      continue;
    }
    const reason = riskReason(candidate);
    if (reason) {
      blocked.push({ id: candidate.id, title: candidate.title, source: candidate.sourceRepository, reason });
      continue;
    }
    itemIds.add(candidate.id);
    promptKeys.add(promptKey);
    identityKeys.add(identityKey);
    candidate.tags = tagsFor(candidate);
    accepted.push(candidate);
  }
  return { accepted, duplicate, blocked };
}

async function loadImage(source) {
  if (/^https?:\/\//i.test(source)) {
    const response = await fetch(source, {
      headers: { "User-Agent": "DAIstudio-prompt-library-sync/1.0" },
      signal: AbortSignal.timeout(30_000),
    });
    if (!response.ok) throw new Error(`HTTP ${response.status}`);
    return Buffer.from(await response.arrayBuffer());
  }
  return source;
}

function extractVideoFrame(source) {
  return execFileSync("ffmpeg", [
    "-loglevel", "error",
    "-i", source,
    "-frames:v", "1",
    "-f", "image2pipe",
    "-vcodec", "png",
    "pipe:1",
  ], {
    encoding: null,
    maxBuffer: 50 * 1024 * 1024,
    stdio: ["ignore", "pipe", "ignore"],
  });
}

async function renderPreview(source, destination, maxPixels) {
  const readsAndWritesSameFile = !/^https?:\/\//i.test(source)
    && path.resolve(source) === path.resolve(destination);
  const input = readsAndWritesSameFile ? await readFile(source) : await loadImage(source);
  const writePreview = (previewInput) => sharp(previewInput, { failOn: "warning" })
      .rotate()
      .resize({ width: maxPixels, height: maxPixels, fit: "inside", withoutEnlargement: true })
      .flatten({ background: "#111111" })
      .jpeg({ quality: 84, chromaSubsampling: "4:4:4", progressive: true, mozjpeg: true })
      .toFile(destination);
  try {
    await writePreview(input);
  } catch (imageError) {
    if (/^https?:\/\//i.test(source)) throw imageError;
    try {
      await writePreview(extractVideoFrame(source));
    } catch {
      throw imageError;
    }
  }
}

async function runPool(values, concurrency, worker) {
  let cursor = 0;
  const runners = Array.from({ length: Math.min(concurrency, values.length) }, async () => {
    while (cursor < values.length) {
      const index = cursor;
      cursor += 1;
      await worker(values[index], index);
    }
  });
  await Promise.all(runners);
}

function publicItem(candidate) {
  const { imageSource: _imageSource, ...item } = candidate;
  return {
    ...item,
    promptLength: item.prompt.length,
    previewUrl: `/prompt-library/thumbs/${item.id}.jpg`,
  };
}

function updateLibraryShape(library, items, sourceRevisions, syncStats) {
  const categories = library.categories.map((category) => ({
    ...category,
    count: items.filter((item) => item.category === category.id).length,
  }));
  const images = items.reduce((sum, item) => sum + Math.max(1, Number(item.imageCount || 0)), 0);
  return {
    stats: {
      total: items.length,
      categories: categories.length,
      images,
      source: "EvoLinkAI + freestylefly + YouMind OpenLab",
      sourceRevisions,
      sync: syncStats,
      generatedAt: new Date().toISOString(),
    },
    categories,
    items,
  };
}

async function main() {
  const args = parseArgs(process.argv.slice(2));
  const evolink = requiredDirectory(args, "evolink");
  const freestylefly = requiredDirectory(args, "freestylefly");
  const youmind = requiredDirectory(args, "youmind");
  const dryRun = args.flags.has("--dry-run");
  const refreshExisting = !args.flags.has("--skip-existing-previews");
  const maxPixels = Number(args.values.get("--preview-size") || 720);
  if (!Number.isInteger(maxPixels) || maxPixels < 320 || maxPixels > 1200) {
    throw new Error("--preview-size must be an integer between 320 and 1200");
  }

  const library = JSON.parse(await readFile(DATA_PATH, "utf8"));
  const preview = JSON.parse(await readFile(PREVIEW_PATH, "utf8"));
  const existingItems = library.items.map((item) => ({
    ...item,
    sourceRepository: item.sourceRepository || SOURCE_CONFIG.evolink.name,
    sourceLicense: item.sourceLicense || SOURCE_CONFIG.evolink.license,
  }));
  const evo = await parseEvo(evolink);
  const freestyleCandidates = await parseFreestylefly(freestylefly);
  const youMindCandidates = await parseYouMind(youmind);
  const allCandidates = [...evo.candidates, ...freestyleCandidates, ...youMindCandidates];
  const selected = selectCandidates(existingItems, allCandidates);
  const sourceRevisions = Object.fromEntries([
    [SOURCE_CONFIG.evolink.name, gitRevision(evolink)],
    [SOURCE_CONFIG.freestylefly.name, gitRevision(freestylefly)],
    [SOURCE_CONFIG.youmind.name, gitRevision(youmind)],
  ]);

  const sourceCounts = Object.fromEntries(
    Object.values(SOURCE_CONFIG).map((source) => [
      source.name,
      selected.accepted.filter((item) => item.sourceRepository === source.name).length,
    ]),
  );
  const report = {
    existing: existingItems.length,
    scanned: allCandidates.length,
    accepted: selected.accepted.length,
    duplicates: selected.duplicate.length,
    blocked: selected.blocked.length,
    sourceCounts,
    sourceRevisions,
    blockedItems: selected.blocked,
  };
  if (dryRun) {
    const verbose = args.flags.has("--verbose");
    report.acceptedItems = selected.accepted.map(({ id, title, prompt, sourceRepository: source }) => ({
      id,
      title,
      source,
      ...(verbose ? { prompt } : {}),
    }));
    process.stdout.write(`${JSON.stringify(report, null, 2)}\n`);
    return;
  }

  await mkdir(THUMBS_DIR, { recursive: true });
  const failed = [];
  const rendered = [];
  await runPool(selected.accepted, 6, async (candidate) => {
    try {
      const destination = path.join(THUMBS_DIR, `${candidate.id}.jpg`);
      await renderPreview(candidate.imageSource, destination, maxPixels);
      rendered.push(candidate);
    } catch (error) {
      failed.push({ id: candidate.id, title: candidate.title, error: error.message });
    }
  });

  let refreshed = 0;
  const refreshFailures = [];
  const refreshedIds = new Set();
  if (refreshExisting) {
    const refreshable = library.items
      .map((item) => ({ item, source: evo.imageMap.get(`${item.category}:${Number(item.caseId)}`) }))
      .filter(({ source }) => source && existsSync(source));
    await runPool(refreshable, 8, async ({ item, source }) => {
      try {
        await renderPreview(source, path.join(FRONTEND_DIR, "public", item.previewUrl), maxPixels);
        refreshed += 1;
        refreshedIds.add(item.id);
      } catch (error) {
        refreshFailures.push({ id: item.id, error: error.message });
      }
    });
  }

  let normalized = 0;
  const normalizationFailures = [];
  const remainingExisting = library.items.filter((item) => !refreshedIds.has(item.id));
  await runPool(remainingExisting, 8, async (item) => {
    const previewPath = path.join(FRONTEND_DIR, "public", item.previewUrl);
    try {
      let needsNormalization = true;
      try {
        const metadata = await sharp(previewPath).metadata();
        const maxDimension = Math.max(metadata.width || 0, metadata.height || 0);
        needsNormalization = metadata.format !== "jpeg" || maxDimension > maxPixels;
      } catch {
        // renderPreview can recover a video file stored under an image extension.
      }
      if (!needsNormalization) return;
      await renderPreview(previewPath, previewPath, maxPixels);
      normalized += 1;
    } catch (error) {
      normalizationFailures.push({ id: item.id, error: error.message });
    }
  });

  const newItems = rendered.map(publicItem);
  const addedBySource = Object.fromEntries(
    Object.values(SOURCE_CONFIG).map((source) => [
      source.name,
      newItems.filter((item) => item.sourceRepository === source.name).length,
    ]),
  );
  const syncStats = {
    scanned: allCandidates.length,
    added: newItems.length,
    addedBySource,
    duplicatesExcluded: selected.duplicate.length,
    safetyExcluded: selected.blocked.length,
    previewFailures: failed.length,
    existingPreviewsRefreshed: refreshed,
    existingPreviewsNormalized: normalized,
  };
  const updated = updateLibraryShape(library, [...existingItems, ...newItems], sourceRevisions, syncStats);
  const updatedItemsById = new Map(updated.items.map((item) => [item.id, item]));
  const updatedPreview = {
    ...preview,
    stats: updated.stats,
    categories: updated.categories,
    items: preview.items.map((item) => updatedItemsById.get(item.id) || item),
    preview: { ...preview.preview, sourceTotal: updated.stats.total },
  };
  const license = {
    source: "EvoLinkAI + freestylefly + YouMind OpenLab",
    sourceUrl: SOURCE_CONFIG.evolink.url,
    licenseStatus: "mixed_open_source_with_attribution",
    notice: "Curated third-party reference prompts and previews. Source attribution is preserved. Entries with explicit public figures, protected brands/IP, adult content, missing prompts, or missing previews are excluded during sync.",
    sources: Object.values(SOURCE_CONFIG).map((source) => ({
      ...source,
      revision: sourceRevisions[source.name],
    })),
  };

  await writeFile(DATA_PATH, `${JSON.stringify(updated)}\n`);
  await writeFile(PREVIEW_PATH, `${JSON.stringify(updatedPreview)}\n`);
  await writeFile(LICENSE_PATH, `${JSON.stringify(license, null, 2)}\n`);
  process.stdout.write(`${JSON.stringify({ ...report, ...syncStats, failed, refreshFailures, normalizationFailures }, null, 2)}\n`);
}

main().catch((error) => {
  process.stderr.write(`${error.stack || error.message}\n`);
  process.exitCode = 1;
});
