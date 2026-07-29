import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import ts from "typescript";

function read(relativePath) {
  return readFileSync(new URL(`../${relativePath}`, import.meta.url), "utf8");
}

function lineCount(source) {
  return source.split(/\r?\n/).length;
}

function functionSpans(source, fileName) {
  const sourceFile = ts.createSourceFile(
    fileName,
    source,
    ts.ScriptTarget.Latest,
    true,
    ts.ScriptKind.JSX,
  );
  const spans = [];
  function visit(node) {
    if (
      ts.isFunctionDeclaration(node)
      || ts.isFunctionExpression(node)
      || ts.isArrowFunction(node)
      || ts.isMethodDeclaration(node)
    ) {
      const start = sourceFile.getLineAndCharacterOfPosition(node.getStart(sourceFile)).line + 1;
      const end = sourceFile.getLineAndCharacterOfPosition(node.end).line + 1;
      spans.push({ start, end, span: end - start + 1 });
    }
    ts.forEachChild(node, visit);
  }
  visit(sourceFile);
  return spans;
}

function assertFunctionBudget(source, fileName, budget) {
  const oversized = functionSpans(source, fileName).filter(({ span }) => span > budget);
  assert.deepEqual(
    oversized,
    [],
    `${fileName} contains functions over ${budget} lines`,
  );
}

const pageSource = read("app/page.jsx");
const rootSource = read("app/studio/StudioWorkspaceRoot.jsx");
const presentationSource = read("app/studio/buildStudioPresentation.js");
const consoleSource = read("app/studio/StudioCreationConsole.jsx");

assert.ok(lineCount(pageSource) <= 15, "app/page.jsx must stay a thin route entry");
assert.match(pageSource, /import StudioWorkspaceRoot from "\.\/studio\/StudioWorkspaceRoot";/);
assert.doesNotMatch(pageSource, /\b(?:useState|useEffect|useRef|creationController)\b/);

assert.ok(lineCount(rootSource) <= 80, "StudioWorkspaceRoot must stay a thin composition root");
assertFunctionBudget(rootSource, "StudioWorkspaceRoot.jsx", 60);
assert.doesNotMatch(rootSource, /\b(?:useState|useEffect|useRef)\b/);
assert.doesNotMatch(
  rootSource,
  /\b(?:useGenerationSubmit|useReferenceParsing|useMediaUpload|useTaskTracking)\b/,
  "low-level business hooks must remain behind Studio domain hooks",
);
assert.doesNotMatch(
  rootSource,
  /const (?:creation|results|assetPicker)Controller\s*=\s*\{/,
  "controllers must be assembled outside the composition root",
);
assert.match(rootSource, /const presentation = buildStudioPresentation\(\{/);

const expectedHooks = [
  "useStudioFoundation",
  "useStudioModelDomain",
  "useStudioTaskDomain",
  "useStudioReverseOperationBridge",
  "useStudioPromptDomain",
  "useStudioReverseDomain",
  "useStudioReferenceDomain",
  "useStudioGenerationDomain",
  "useStudioOwnerLifecycle",
];
for (const hook of expectedHooks) {
  assert.match(rootSource, new RegExp(`\\b${hook}\\(`), `${hook} must be wired by the root`);
}

const domainFiles = [
  "hooks/studio/useStudioSharedRefs.js",
  "hooks/studio/useStudioFoundation.js",
  "hooks/studio/useStudioModelDomain.js",
  "hooks/studio/useStudioTaskDomain.js",
  "hooks/studio/useStudioPromptDomain.js",
  "hooks/studio/useStudioReverseDomain.js",
  "hooks/studio/useStudioReferenceDomain.js",
  "hooks/studio/useStudioGenerationDomain.js",
  "hooks/studio/useStudioOwnerLifecycle.js",
];
for (const fileName of domainFiles) {
  const source = read(fileName);
  assert.ok(lineCount(source) <= 330, `${fileName} exceeds the domain module budget`);
  assertFunctionBudget(source, fileName, 300);
}

assert.ok(
  lineCount(presentationSource) <= 400,
  "buildStudioPresentation.js exceeds the presentation assembly budget",
);
assertFunctionBudget(presentationSource, "buildStudioPresentation.js", 180);
assert.doesNotMatch(
  presentationSource,
  /\b(?:useState|useEffect|useRef)\b/,
  "presentation assembly must remain pure",
);

const controllerStart = presentationSource.indexOf("  return {\n    mode: {");
const controllerEnd = presentationSource.indexOf(
  "\n}\n\nfunction buildReferenceController",
  controllerStart,
);
assert.ok(controllerStart >= 0 && controllerEnd > controllerStart, "creation controller contract is missing");
const controllerSource = presentationSource.slice(controllerStart, controllerEnd);
const domainKeys = [...controllerSource.matchAll(
  /^    ([a-zA-Z]\w*): (?:\{|build[A-Z]\w+Controller\()/gm,
)]
  .map((match) => match[1]);
const expectedDomains = [
  "mode",
  "prompt",
  "workspace",
  "generation",
  "reference",
  "reverse",
  "ui",
];
assert.deepEqual(domainKeys, expectedDomains, "creation controller must stay grouped by domain");
for (const domain of expectedDomains) {
  assert.match(
    consoleSource,
    new RegExp(`(?:${domain}: \\w+Controller|${domain},)`),
    `StudioCreationConsole must consume the ${domain} controller domain`,
  );
}

console.log("studio architecture checks passed");
