/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    "./app/**/*.{js,jsx,ts,tsx}",
    "./components/**/*.{js,jsx,ts,tsx}",
    "./hooks/**/*.{js,jsx,ts,tsx}",
    "./lib/**/*.{js,jsx,ts,tsx}",
  ],
  theme: {
    extend: {
      colors: {
        // Deep, slightly-violet dark base (Dreamina / 即梦 inspired)
        base: "#07070e",
        base2: "#0c0c16",
        surface: "#12121f",
        surface2: "#181826",
        raised: "#20202f",
        line: "rgba(255,255,255,0.08)",
        line2: "rgba(255,255,255,0.14)",
        // Text
        snow: "#f4f4fb", // primary
        mist: "#b8b8cb", // secondary
        fog: "#7b7b90", // muted
        // Neon accents
        iris: { DEFAULT: "#7b61ff", 400: "#9d86ff", 600: "#6044e6" },
        rose: "#ff5fa2",
        aqua: "#34e0ff",
        // status
        ok: "#4ade80",
        warn: "#fbbf24",
        bad: "#fb7185",
      },
      fontFamily: {
        display: ["Sora", "Noto Sans SC", "system-ui", "sans-serif"],
        body: ["Noto Sans SC", "Inter", "system-ui", "sans-serif"],
      },
      boxShadow: {
        glow: "0 8px 40px rgba(123,97,255,0.35)",
        "glow-sm": "0 4px 18px rgba(123,97,255,0.30)",
        card: "0 1px 0 rgba(255,255,255,0.04) inset, 0 18px 50px rgba(0,0,0,0.45)",
        pop: "0 30px 80px rgba(0,0,0,0.65)",
      },
      borderRadius: {
        xl2: "1rem",
        xl3: "1.5rem",
        xl4: "2rem",
      },
      backgroundImage: {
        brand: "linear-gradient(100deg,#7b61ff 0%,#b65cff 46%,#ff5fa2 100%)",
        "brand-soft":
          "linear-gradient(100deg,rgba(123,97,255,0.18),rgba(255,95,162,0.14))",
      },
      keyframes: {
        shimmer: {
          "100%": { transform: "translateX(100%)" },
        },
        floaty: {
          "0%,100%": { transform: "translateY(0)" },
          "50%": { transform: "translateY(-8px)" },
        },
        fadeup: {
          "0%": { opacity: 0, transform: "translateY(12px)" },
          "100%": { opacity: 1, transform: "translateY(0)" },
        },
        glowpulse: {
          "0%,100%": { opacity: 0.5 },
          "50%": { opacity: 1 },
        },
      },
      animation: {
        shimmer: "shimmer 1.6s infinite",
        floaty: "floaty 6s ease-in-out infinite",
        fadeup: "fadeup 0.5s ease-out both",
        glowpulse: "glowpulse 2.6s ease-in-out infinite",
      },
    },
  },
  plugins: [],
};
