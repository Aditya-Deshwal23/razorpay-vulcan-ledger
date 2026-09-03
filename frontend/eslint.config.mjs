import nextVitals from "eslint-config-next/core-web-vitals";

const config = [
  ...nextVitals,
  {
    rules: {
      // Fetching a resource in an effect necessarily marks it pending before
      // the async response resolves. This is an intentional loading-state
      // transition, not a derived-value update.
      "react-hooks/set-state-in-effect": "off",
    },
  },
];

export default config;
