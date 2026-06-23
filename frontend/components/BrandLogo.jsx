"use client";

import { useId } from "react";

export default function BrandLogo({ className = "", title = "造梦 Studio" }) {
  const id = useId().replace(/:/g, "");
  const titleId = title ? `${id}-brand-logo-title` : undefined;
  const bgId = `${id}-dream-logo-bg`;
  const glowId = `${id}-dream-logo-glow`;
  const markId = `${id}-dream-logo-mark`;
  const shadowId = `${id}-dream-logo-shadow`;

  return (
    <svg viewBox="0 0 64 64" className={className} role={title ? "img" : "presentation"} aria-labelledby={titleId}>
      {title ? <title id={titleId}>{title}</title> : null}
      <defs>
        <linearGradient id={bgId} x1="8" y1="7" x2="56" y2="57" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#7b61ff" />
          <stop offset="0.48" stopColor="#b65cff" />
          <stop offset="1" stopColor="#ff5fa2" />
        </linearGradient>
        <radialGradient id={glowId} cx="0" cy="0" r="1" gradientUnits="userSpaceOnUse" gradientTransform="translate(20 16) rotate(46) scale(46)">
          <stop offset="0" stopColor="#ffffff" stopOpacity="0.34" />
          <stop offset="0.55" stopColor="#ffffff" stopOpacity="0.08" />
          <stop offset="1" stopColor="#ffffff" stopOpacity="0" />
        </radialGradient>
        <linearGradient id={markId} x1="17" y1="16" x2="47" y2="49" gradientUnits="userSpaceOnUse">
          <stop offset="0" stopColor="#ffffff" />
          <stop offset="0.72" stopColor="#f4eaff" />
          <stop offset="1" stopColor="#bdf6ff" />
        </linearGradient>
        <filter id={shadowId} x="-20%" y="-20%" width="140%" height="140%" colorInterpolationFilters="sRGB">
          <feDropShadow dx="0" dy="8" stdDeviation="6" floodColor="#7b61ff" floodOpacity="0.34" />
        </filter>
      </defs>
      <rect x="5" y="5" width="54" height="54" rx="18" fill="#07070e" />
      <rect x="6" y="6" width="52" height="52" rx="17" fill={`url(#${bgId})`} filter={`url(#${shadowId})`} />
      <rect x="6" y="6" width="52" height="52" rx="17" fill={`url(#${glowId})`} />
      <path d="M18 24.5c0-3.59 2.91-6.5 6.5-6.5h15c3.59 0 6.5 2.91 6.5 6.5v15c0 3.59-2.91 6.5-6.5 6.5h-15c-3.59 0-6.5-2.91-6.5-6.5v-15Z" fill="#080812" fillOpacity="0.24" stroke="#fff" strokeOpacity="0.48" strokeWidth="2" />
      <path d="M31.75 16.5c1.52 7.16 5.08 11.15 12.18 12.9-7.1 1.75-10.66 5.74-12.18 12.9-1.52-7.16-5.08-11.15-12.18-12.9 7.1-1.75 10.66-5.74 12.18-12.9Z" fill={`url(#${markId})`} />
      <path d="M43.6 36.2c.7 3.29 2.34 5.12 5.6 5.92-3.26.81-4.9 2.64-5.6 5.92-.7-3.28-2.34-5.11-5.6-5.92 3.26-.8 4.9-2.63 5.6-5.92Z" fill="#fff" fillOpacity="0.9" />
      <path d="M22.1 13.6c.43 2.05 1.46 3.2 3.5 3.7-2.04.51-3.07 1.65-3.5 3.7-.44-2.05-1.46-3.19-3.5-3.7 2.04-.5 3.06-1.65 3.5-3.7Z" fill="#fff" fillOpacity="0.82" />
      <path d="M22.5 42.4 29 35.1l4.35 4.76 3.35-3.45 5.8 5.99" fill="none" stroke="#fff" strokeOpacity="0.9" strokeWidth="2.2" strokeLinecap="round" strokeLinejoin="round" />
      <circle cx="40.5" cy="23.5" r="2.2" fill="#fff" fillOpacity="0.92" />
    </svg>
  );
}
