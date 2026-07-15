/**
 * @file vitest-lite.ts
 * @brief 为独立 tsx 测试提供最小 describe/it/expect 兼容层。
 *
 * 该插件测试由 scripts/run-tests.mjs 直接执行 *.test.ts。少数历史测试
 * 曾引用 vitest，但本包未声明该依赖；这里仅实现这些测试实际使用的断言。
 */
import assert from "node:assert/strict";
let passed = 0;
let failed = 0;
let chain = Promise.resolve();
let summaryScheduled = false;
function ScheduleSummary() {
    if (summaryScheduled) {
        return;
    }
    summaryScheduled = true;
    setImmediate(async () => {
        await chain;
        console.log(`\n${passed} passed, ${failed} failed`);
        if (failed > 0) {
            process.exitCode = 1;
        }
    });
}
export function describe(name, fn) {
    console.log(`\n=== ${name} ===\n`);
    fn();
}
export function it(name, fn) {
    chain = chain.then(async () => {
        try {
            await fn();
            passed++;
            console.log(`  PASS  ${name}`);
        }
        catch (e) {
            failed++;
            console.error(`  FAIL  ${name}`);
            console.error(`        ${e.message}`);
            if (e.stack) {
                console.error(e.stack.split("\n").slice(1, 4).join("\n"));
            }
        }
    });
    ScheduleSummary();
}
function CheckNegated(negated, positive, negative) {
    if (negated) {
        negative();
        return;
    }
    positive();
}
function HasValue(actual, expected) {
    if (typeof actual === "string") {
        return actual.includes(String(expected));
    }
    if (Array.isArray(actual)) {
        return actual.includes(expected);
    }
    if (actual !== null
        && actual !== undefined
        && typeof actual.includes === "function") {
        return Boolean(actual
            .includes(expected));
    }
    return false;
}
function Format(value) {
    return typeof value === "string" ? value : JSON.stringify(value);
}
function BuildMatchers(actual, negated = false) {
    const matchers = {
        toBe(expected) {
            CheckNegated(negated, () => assert.strictEqual(actual, expected), () => assert.notStrictEqual(actual, expected));
        },
        toEqual(expected) {
            CheckNegated(negated, () => assert.deepStrictEqual(actual, expected), () => assert.notDeepStrictEqual(actual, expected));
        },
        toContain(expected) {
            const contains = HasValue(actual, expected);
            if (negated) {
                assert.equal(contains, false, `expected ${Format(actual)} not to contain `
                    + `${Format(expected)}`);
                return;
            }
            assert.equal(contains, true, `expected ${Format(actual)} to contain ${Format(expected)}`);
        },
        toHaveLength(expected) {
            const length = actual?.length;
            CheckNegated(negated, () => assert.strictEqual(length, expected), () => assert.notStrictEqual(length, expected));
        },
        toBeNull() {
            CheckNegated(negated, () => assert.strictEqual(actual, null), () => assert.notStrictEqual(actual, null));
        },
        toBeDefined() {
            CheckNegated(negated, () => assert.notStrictEqual(actual, undefined), () => assert.strictEqual(actual, undefined));
        },
        toBeUndefined() {
            CheckNegated(negated, () => assert.strictEqual(actual, undefined), () => assert.notStrictEqual(actual, undefined));
        },
        toBeTruthy() {
            CheckNegated(negated, () => assert.equal(Boolean(actual), true), () => assert.equal(Boolean(actual), false));
        },
        toBeFalsy() {
            CheckNegated(negated, () => assert.equal(Boolean(actual), false), () => assert.equal(Boolean(actual), true));
        },
        toBeGreaterThan(expected) {
            CheckNegated(negated, () => assert.ok(Number(actual) > expected), () => assert.ok(!(Number(actual) > expected)));
        },
        toBeGreaterThanOrEqual(expected) {
            CheckNegated(negated, () => assert.ok(Number(actual) >= expected), () => assert.ok(!(Number(actual) >= expected)));
        },
        toBeLessThan(expected) {
            CheckNegated(negated, () => assert.ok(Number(actual) < expected), () => assert.ok(!(Number(actual) < expected)));
        },
        toBeLessThanOrEqual(expected) {
            CheckNegated(negated, () => assert.ok(Number(actual) <= expected), () => assert.ok(!(Number(actual) <= expected)));
        },
        toThrow(expected) {
            assert.equal(typeof actual, "function");
            let thrown;
            try {
                actual();
            }
            catch (e) {
                thrown = e;
            }
            const message = thrown instanceof Error
                ? thrown.message
                : String(thrown);
            const matches = thrown !== undefined
                && (expected === undefined
                    || (typeof expected === "string"
                        && message.includes(expected))
                    || (expected instanceof RegExp && expected.test(message)));
            if (negated) {
                assert.equal(matches, false);
                return;
            }
            assert.equal(matches, true, "expected function to throw");
        },
    };
    Object.defineProperty(matchers, "not", {
        get: () => BuildMatchers(actual, !negated),
    });
    return matchers;
}
export function expect(actual) {
    return BuildMatchers(actual);
}
