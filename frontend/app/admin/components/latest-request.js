export function createLatestRequestGate() {
  let generation = 0;

  return {
    begin() {
      generation += 1;
      return generation;
    },
    isCurrent(requestGeneration) {
      return requestGeneration === generation;
    },
    invalidate() {
      generation += 1;
    },
  };
}
