import assert from "node:assert/strict";
import test from "node:test";

import {
  createVideoSourceRange,
  moveVideoSourceRange,
  resizeVideoSourceRange,
  toggleVideoTimelineKeyframe,
  videoTimelineConfigIssues,
  videoTimelineConfigPatch,
  videoTimelineFrameTimes,
} from "../app/studio/videoSourceTimeline.ts";

test("video source ranges remain ordered, separate and bounded while dragging", () => {
  const source = [
    { start_seconds: 1, end_seconds: 4 },
    { start_seconds: 6, end_seconds: 9 },
  ];

  assert.deepEqual(moveVideoSourceRange(source, 10, 0, 4), [
    { start_seconds: 2.9, end_seconds: 5.9 },
    { start_seconds: 6, end_seconds: 9 },
  ]);
  assert.deepEqual(resizeVideoSourceRange(source, 10, 1, "start", 2), [
    { start_seconds: 1, end_seconds: 4 },
    { start_seconds: 4.1, end_seconds: 9 },
  ]);
  assert.deepEqual(resizeVideoSourceRange(source, 10, 0, "end", 8), [
    { start_seconds: 1, end_seconds: 5.9 },
    { start_seconds: 6, end_seconds: 9 },
  ]);
});

test("dragging on empty timeline creates a normalized range", () => {
  assert.deepEqual(createVideoSourceRange(8.4, 2.2, 12), {
    start_seconds: 2.2,
    end_seconds: 8.4,
  });
  assert.equal(createVideoSourceRange(4, 4.02, 12), null);
  assert.deepEqual(createVideoSourceRange(-3, 20, 12), {
    start_seconds: 0,
    end_seconds: 12,
  });
});

test("timeline config patch mirrors request fields and prunes out-of-range keyframes", () => {
  assert.deepEqual(videoTimelineConfigPatch(
    [
      { start_seconds: 0, end_seconds: 3 },
      { start_seconds: 2, end_seconds: 5 },
      { start_seconds: 8, end_seconds: 9 },
    ],
    [0, 4, 7, 8.5, 20],
    10,
  ), {
    source_range: null,
    source_ranges: [
      { start_seconds: 0, end_seconds: 5 },
      { start_seconds: 8, end_seconds: 9 },
    ],
    custom_keyframes: [0, 4, 8.5],
  });

  assert.deepEqual(videoTimelineConfigPatch(
    [{ start_seconds: 1, end_seconds: 3 }],
    [1.5],
    10,
  ).source_range, { start_seconds: 1, end_seconds: 3 });
});

test("timeline click toggles keyframes only inside selected ranges", () => {
  const ranges = [{ start_seconds: 2, end_seconds: 6 }];
  assert.deepEqual(toggleVideoTimelineKeyframe(4.26, [4.2], 10, ranges), []);
  assert.deepEqual(toggleVideoTimelineKeyframe(4.4, [4.2], 10, ranges), [4.2, 4.4]);
  assert.deepEqual(toggleVideoTimelineKeyframe(7, [4.2], 10, ranges), [4.2]);
});

test("thumbnail capture times are deterministic and avoid undecodable end frames", () => {
  assert.deepEqual(videoTimelineFrameTimes(10, 4), [1.25, 3.75, 6.25, 8.75]);
  assert.deepEqual(videoTimelineFrameTimes(0, 4), []);
});

test("invalid restored timeline values remain visible as errors before normalization", () => {
  assert.deepEqual(videoTimelineConfigIssues(
    [
      { start_seconds: 0, end_seconds: 4 },
      { start_seconds: 3, end_seconds: 8 },
      { start_seconds: 13, end_seconds: 14 },
    ],
    [2, 11, -1],
    10,
  ).map((issue) => issue.message), [
    "分析片段不能超过当前视频时长。",
    "分析片段不能重叠；请拖动边界分开或删除重复片段。",
    "关键帧必须位于当前视频时长范围内。",
  ]);
});
