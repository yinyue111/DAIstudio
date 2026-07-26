import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

import {
  cloneStructuredValue,
  emptyStructuredListItem,
  isStructuredFieldEnvelope,
  parseStructuredNumberInput,
  removeStructuredListItem,
  reviveStructuredValue,
  setStructuredValueAtPath,
  structuredEnumOptions,
  structuredFieldKind,
  structuredFieldMetadata,
  structuredFieldValue,
  structuredPathKey,
  structuredValueAtPath,
  structuredValuesEqual,
} from "../app/studio/structuredEditor.ts";

const root = dirname(dirname(fileURLToPath(import.meta.url)));
const editorSource = readFileSync(join(root, "app/studio/StudioStructuredEditor.jsx"), "utf8");

// ---- envelope helpers ----
const envelope = { value: "cinematic", type: "enum", options: ["cinematic", "flat"] };
assert.equal(isStructuredFieldEnvelope(envelope), true);
assert.equal(isStructuredFieldEnvelope({ value: 1 }), false, "value alone is not an envelope");
assert.equal(isStructuredFieldEnvelope("cinematic"), false);
assert.equal(structuredFieldValue(envelope), "cinematic");
assert.equal(structuredFieldValue("raw"), "raw");

// ---- clone / equality ----
const nested = { shots: [{ camera: "pan", duration: 2 }] };
const cloned = cloneStructuredValue(nested);
assert.deepEqual(cloned, nested);
assert.notEqual(cloned.shots, nested.shots, "clone must be deep");
assert.notEqual(cloned.shots[0], nested.shots[0]);
assert.equal(structuredValuesEqual({ value: [1, 2], type: "list" }, [1, 2]), true);
assert.equal(structuredValuesEqual("a", "b"), false);

// ---- path read ----
const structuredRoot = {
  "镜头列表": {
    value: [{ "运镜": "推进", "时长": 2 }, { "运镜": "环绕", "时长": 3 }],
    type: "list",
  },
  "光线": "黄昏逆光",
};
assert.equal(structuredValueAtPath(structuredRoot, ["镜头列表", 1, "运镜"]), "环绕");
assert.equal(structuredValueAtPath(structuredRoot, ["光线"]), "黄昏逆光");
assert.equal(structuredValueAtPath(structuredRoot, ["不存在", 0]), undefined);

// ---- path write keeps envelopes and immutability ----
const written = setStructuredValueAtPath(structuredRoot, ["镜头列表", 0, "时长"], 5);
assert.equal(structuredValueAtPath(written, ["镜头列表", 0, "时长"]), 5);
assert.equal(structuredValueAtPath(structuredRoot, ["镜头列表", 0, "时长"]), 2, "original untouched");
assert.equal(written["镜头列表"].type, "list", "envelope metadata preserved on write");
assert.equal(typeof structuredValueAtPath(written, ["镜头列表", 0, "时长"]), "number", "no string degradation");

// empty path replaces the value but keeps the envelope wrapper
const replaced = setStructuredValueAtPath(envelope, [], "flat");
assert.deepEqual(replaced, { ...envelope, value: "flat" });

// ---- list add/remove ----
const removed = removeStructuredListItem(structuredRoot, ["镜头列表"], 0);
assert.equal(structuredFieldValue(removed["镜头列表"]).length, 1);
assert.equal(structuredFieldValue(removed["镜头列表"])[0]["运镜"], "环绕");
const removedNoList = removeStructuredListItem(structuredRoot, ["光线"], 0);
assert.deepEqual(removedNoList, structuredRoot, "removing from a non-list is a safe no-op");

const shotList = structuredFieldValue(structuredRoot["镜头列表"]);
const appended = setStructuredValueAtPath(
  structuredRoot,
  ["镜头列表", shotList.length],
  emptyStructuredListItem(shotList, {}),
);
assert.equal(structuredFieldValue(appended["镜头列表"]).length, 3);
assert.deepEqual(structuredFieldValue(appended["镜头列表"])[2], {}, "sample-based empty item is an object");
assert.equal(emptyStructuredListItem([], { item_type: "number" }), 0);
assert.equal(emptyStructuredListItem([], { item_default: { camera: "static" } }).camera, "static");
assert.equal(emptyStructuredListItem([true], {}), false);
assert.equal(emptyStructuredListItem([], {}), "");

// ---- kind / enum metadata ----
assert.equal(structuredFieldKind([1, 2]), "list");
assert.equal(structuredFieldKind({ a: 1 }), "object");
assert.equal(structuredFieldKind(3), "number");
assert.equal(structuredFieldKind(true), "boolean");
assert.equal(structuredFieldKind("x"), "string");
assert.equal(structuredFieldKind("x", { enum: ["x", "y"] }), "enum");
assert.equal(structuredFieldKind([], { type: "array" }), "list");
assert.deepEqual(
  structuredEnumOptions({ options: [{ value: "a", label: "甲" }, "b"] }),
  [{ value: "a", label: "甲" }, { value: "b", label: "b" }],
);
assert.deepEqual(structuredEnumOptions({}), []);

// metadata precedence: embedded envelope < explicit map entries
const metadataMap = { "/镜头列表": { label: "分镜" }, "光线": { type: "string" } };
assert.equal(structuredFieldMetadata(metadataMap, ["镜头列表"], structuredRoot["镜头列表"]).label, "分镜");
assert.equal(structuredFieldMetadata(metadataMap, ["镜头列表"], structuredRoot["镜头列表"]).type, "list");
assert.equal(structuredFieldMetadata(metadataMap, ["光线"], "黄昏逆光").type, "string");

// ---- path key escaping ----
assert.equal(structuredPathKey(["a/b", 0, "c~d"]), "a~1b/0/c~0d");

// ---- degraded history revive (backward compatibility) ----
const degradedList = JSON.stringify([{ "运镜": "推进" }]);
assert.deepEqual(reviveStructuredValue(degradedList), [{ "运镜": "推进" }]);
assert.deepEqual(reviveStructuredValue(`  ${JSON.stringify({ a: 1 })}  `), { a: 1 });
assert.equal(reviveStructuredValue("黄昏逆光"), "黄昏逆光", "plain prose stays a string");
assert.equal(reviveStructuredValue("{broken json"), "{broken json", "broken JSON never throws");
assert.equal(reviveStructuredValue("[not json]"), "[not json]", "unparsable bracket text stays a string");
assert.equal(reviveStructuredValue("123"), "123", "scalar-looking strings are not coerced");
assert.equal(reviveStructuredValue(7), 7);
const degradedEnvelope = { value: degradedList, type: "list" };
assert.deepEqual(reviveStructuredValue(degradedEnvelope), { value: [{ "运镜": "推进" }], type: "list" });
assert.equal(reviveStructuredValue(envelope), envelope, "clean envelopes are returned as-is");

// editing a revived structure writes back real structure, not a string
const revived = reviveStructuredValue(degradedList);
const editedRevived = setStructuredValueAtPath(revived, [0, "运镜"], "环绕");
assert.deepEqual(editedRevived, [{ "运镜": "环绕" }]);

// ---- number input parsing ----
assert.equal(parseStructuredNumberInput("3.5", 0), 3.5);
assert.equal(parseStructuredNumberInput(" 42 ", 0), 42);
assert.equal(parseStructuredNumberInput("", 7), 7, "clearing the field keeps the previous value");
assert.equal(parseStructuredNumberInput("abc", 7), 7, "invalid input keeps the previous value");
assert.equal(parseStructuredNumberInput("1e3", 7), 1000);

// ---- editor wiring: the JSX editor must consume the structured helpers ----
assert.match(
  editorSource,
  /from "\.\/structuredEditor"/,
  "StudioStructuredEditor must use the structured editing helpers",
);
assert.match(editorSource, /reviveStructuredValue\(/, "degraded history values must be revived before editing");
assert.match(editorSource, /setStructuredValueAtPath\(/, "edits must write back through structured paths");
assert.match(editorSource, /removeStructuredListItem\(/, "list items must be removable");
assert.match(editorSource, /emptyStructuredListItem\(/, "list items must be addable");
assert.doesNotMatch(
  editorSource,
  /value=\{typeof structured\[key\] === "string" \? structured\[key\] : JSON\.stringify/,
  "nested dimensions must not be flattened into JSON strings",
);
assert.match(editorSource, />\s*应用结构修改\s*</);
assert.match(editorSource, />\s*撤销结构修改\s*</);
assert.match(editorSource, /反推维度/);

console.log("structured-editor.test.mjs passed");
