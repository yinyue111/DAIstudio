"use client";

export default function StudioMessageBar({ message }) {
  if (!message) return null;

  return (
    <div className="mt-3 rounded-xl border border-bad/30 bg-bad/10 px-4 py-2.5 text-sm text-bad shadow-pop max-lg:fixed max-lg:inset-x-3 max-lg:bottom-[76px] max-lg:z-30 max-lg:mt-0">
      {message}
    </div>
  );
}
