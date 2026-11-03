import { describe, expect, it } from "vitest";
import { createRequire } from "node:module";

type Qs = {
  stringify: (value: unknown, options?: {
    arrayFormat?: string;
    encodeValuesOnly?: boolean;
  }) => string;
};

const requireFromTest = createRequire(import.meta.url);
const requireFromExpress = createRequire(requireFromTest.resolve("express/package.json"));
const qs = requireFromExpress("qs") as Qs;

describe("qs comma serializer dependency security (GHSA-6pq8-jhm2-2vcw)", () => {
  it.each([false, true])(
    "does not throw for a null-prototype array element with non-callable toString (encodeValuesOnly: %s)",
    (encodeValuesOnly) => {
      const element = Object.create(null) as Record<string, unknown>;
      element.toString = "shadowed";

      expect(() => qs.stringify({ a: [element] }, {
        arrayFormat: "comma",
        encodeValuesOnly,
      })).not.toThrow();
      expect(qs.stringify({ a: [element] }, {
        arrayFormat: "comma",
        encodeValuesOnly,
      })).toBe("a=%5Bobject%20Object%5D");
    },
  );

  it("does not throw for nested arrays with a shadowed non-callable toString", () => {
    const element = Object.create(null) as Record<string, unknown>;
    element.toString = null;

    expect(qs.stringify({ a: [[element]] }, { arrayFormat: "comma" })).toBe(
      "a=%5Bobject%20Object%5D",
    );
  });

  it("preserves ordinary comma serialization", () => {
    expect(qs.stringify({ a: ["alpha", "beta"] }, { arrayFormat: "comma" })).toBe(
      "a=alpha%2Cbeta",
    );
  });

  it("preserves scalar serialization with comma mode", () => {
    expect(qs.stringify({ a: "alpha" }, { arrayFormat: "comma" })).toBe("a=alpha");
  });
});
