/**
 * @file vitest-lite.ts
 * @brief 为独立 tsx 测试提供最小 describe/it/expect 兼容层。
 *
 * 该插件测试由 scripts/run-tests.mjs 直接执行 *.test.ts。少数历史测试
 * 曾引用 vitest，但本包未声明该依赖；这里仅实现这些测试实际使用的断言。
 */

import assert from "node:assert/strict";

type TestFn = () => unknown | Promise<unknown>;

interface Matchers {
    not: Matchers;
    toBe: (expected: unknown) => void;
    toEqual: (expected: unknown) => void;
    toContain: (expected: unknown) => void;
    toHaveLength: (expected: number) => void;
    toBeNull: () => void;
    toBeDefined: () => void;
    toBeUndefined: () => void;
    toBeTruthy: () => void;
    toBeFalsy: () => void;
    toBeGreaterThan: (expected: number) => void;
    toBeGreaterThanOrEqual: (expected: number) => void;
    toBeLessThan: (expected: number) => void;
    toBeLessThanOrEqual: (expected: number) => void;
    toThrow: (expected?: RegExp | string) => void;
}

let passed = 0;
let failed = 0;
let chain: Promise<void> = Promise.resolve();
let summaryScheduled = false;

function ScheduleSummary(): void {
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

export function describe(name: string, fn: () => void): void {
    console.log(`\n=== ${name} ===\n`);
    fn();
}

export function it(name: string, fn: TestFn): void {
    chain = chain.then(async () => {
        try {
            await fn();
            passed++;
            console.log(`  PASS  ${name}`);
        } catch (e) {
            failed++;
            console.error(`  FAIL  ${name}`);
            console.error(`        ${(e as Error).message}`);
            if ((e as Error).stack) {
                console.error(
                    (e as Error).stack!.split("\n").slice(1, 4).join("\n"),
                );
            }
        }
    });
    ScheduleSummary();
}

function CheckNegated(
    negated: boolean,
    positive: () => void,
    negative: () => void,
): void {
    if (negated) {
        negative();
        return;
    }
    positive();
}

function HasValue(actual: unknown, expected: unknown): boolean {
    if (typeof actual === "string") {
        return actual.includes(String(expected));
    }
    if (Array.isArray(actual)) {
        return actual.includes(expected);
    }
    if (
        actual !== null
        && actual !== undefined
        && typeof (actual as { includes?: unknown }).includes === "function"
    ) {
        return Boolean(
            (actual as { includes: (value: unknown) => boolean })
                .includes(expected),
        );
    }
    return false;
}

function Format(value: unknown): string {
    return typeof value === "string" ? value : JSON.stringify(value);
}

function BuildMatchers(actual: unknown, negated = false): Matchers {
    const matchers = {
        toBe(expected: unknown): void {
            CheckNegated(
                negated,
                () => assert.strictEqual(actual, expected),
                () => assert.notStrictEqual(actual, expected),
            );
        },

        toEqual(expected: unknown): void {
            CheckNegated(
                negated,
                () => assert.deepStrictEqual(actual, expected),
                () => assert.notDeepStrictEqual(actual, expected),
            );
        },

        toContain(expected: unknown): void {
            const contains = HasValue(actual, expected);
            if (negated) {
                assert.equal(
                    contains,
                    false,
                    `expected ${Format(actual)} not to contain `
                        + `${Format(expected)}`,
                );
                return;
            }
            assert.equal(
                contains,
                true,
                `expected ${Format(actual)} to contain ${Format(expected)}`,
            );
        },

        toHaveLength(expected: number): void {
            const length = (actual as { length?: unknown } | null)?.length;
            CheckNegated(
                negated,
                () => assert.strictEqual(length, expected),
                () => assert.notStrictEqual(length, expected),
            );
        },

        toBeNull(): void {
            CheckNegated(
                negated,
                () => assert.strictEqual(actual, null),
                () => assert.notStrictEqual(actual, null),
            );
        },

        toBeDefined(): void {
            CheckNegated(
                negated,
                () => assert.notStrictEqual(actual, undefined),
                () => assert.strictEqual(actual, undefined),
            );
        },

        toBeUndefined(): void {
            CheckNegated(
                negated,
                () => assert.strictEqual(actual, undefined),
                () => assert.notStrictEqual(actual, undefined),
            );
        },

        toBeTruthy(): void {
            CheckNegated(
                negated,
                () => assert.equal(Boolean(actual), true),
                () => assert.equal(Boolean(actual), false),
            );
        },

        toBeFalsy(): void {
            CheckNegated(
                negated,
                () => assert.equal(Boolean(actual), false),
                () => assert.equal(Boolean(actual), true),
            );
        },

        toBeGreaterThan(expected: number): void {
            CheckNegated(
                negated,
                () => assert.ok(Number(actual) > expected),
                () => assert.ok(!(Number(actual) > expected)),
            );
        },

        toBeGreaterThanOrEqual(expected: number): void {
            CheckNegated(
                negated,
                () => assert.ok(Number(actual) >= expected),
                () => assert.ok(!(Number(actual) >= expected)),
            );
        },

        toBeLessThan(expected: number): void {
            CheckNegated(
                negated,
                () => assert.ok(Number(actual) < expected),
                () => assert.ok(!(Number(actual) < expected)),
            );
        },

        toBeLessThanOrEqual(expected: number): void {
            CheckNegated(
                negated,
                () => assert.ok(Number(actual) <= expected),
                () => assert.ok(!(Number(actual) <= expected)),
            );
        },

        toThrow(expected?: RegExp | string): void {
            assert.equal(typeof actual, "function");
            let thrown: unknown;
            try {
                (actual as () => unknown)();
            } catch (e) {
                thrown = e;
            }
            const message = thrown instanceof Error
                ? thrown.message
                : String(thrown);
            const matches = thrown !== undefined
                && (
                    expected === undefined
                    || (
                        typeof expected === "string"
                        && message.includes(expected)
                    )
                    || (expected instanceof RegExp && expected.test(message))
                );
            if (negated) {
                assert.equal(matches, false);
                return;
            }
            assert.equal(matches, true, "expected function to throw");
        },
    } as Matchers;

    Object.defineProperty(matchers, "not", {
        get: () => BuildMatchers(actual, !negated),
    });

    return matchers;
}

export function expect(actual: unknown): Matchers {
    return BuildMatchers(actual);
}
