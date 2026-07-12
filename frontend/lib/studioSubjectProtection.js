export function startSubjectProtectionPreview({ load, onSuccess, onError }) {
  let canceled = false;
  const settled = Promise.resolve()
    .then(load)
    .then(
      (result) => {
        if (!canceled) onSuccess(result);
      },
      (error) => {
        if (!canceled) onError(error);
      },
    );

  return {
    cancel() {
      canceled = true;
    },
    settled,
  };
}
