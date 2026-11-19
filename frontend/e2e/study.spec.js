import { expect, test } from "@playwright/test"
import { mockApi } from "./mockApi"

test.describe("Library", () => {
  test("shows each document's sections, progress and one next task, with no Study button", async ({ page }) => {
    await mockApi(page)
    await page.goto("/")
    await page.getByRole("button", { name: "Library", exact: true }).click()

    await expect(page.getByText("print-culture.pdf")).toBeVisible()
    await expect(page.getByRole("listitem", { name: /1\. Print Arrives · pp\. 1–6 · not started/ })).toBeVisible()
    await expect(page.getByRole("list", { name: "Sections" }).getByRole("listitem")).toHaveCount(4)
    await expect(page.getByText("0 of 4 sections studied")).toBeVisible()
    await expect(page.getByRole("button", { name: /Get an overview/ })).toBeVisible()
    await expect(page.getByRole("button", { name: "Study", exact: true })).toHaveCount(1) // the nav tab only
  })
})

test.describe("Study from a suggested task", () => {
  test("runs the task, streams the outputs, saves quiz answers and updates the Library", async ({ page }) => {
    // A short window, so the sticky message bar sits over the end of the conversation.
    await page.setViewportSize({ width: 1100, height: 650 })
    const api = await mockApi(page)
    await page.goto("/")
    await page.getByRole("button", { name: "Library", exact: true }).click()
    await page.getByRole("button", { name: /Get an overview/ }).click()

    await expect(page.getByRole("heading", { name: "print-culture.pdf" })).toBeVisible()
    await expect(page.getByText("Literacy grew")).toBeVisible()
    expect(api.sentMessages).toEqual(["Give me an overview of the whole document in 5 bullet points"])

    // Questions are numbered within the quiz, as the planner counts them.
    await expect(page.getByText("2.", { exact: true })).toBeVisible()
    await page.getByRole("button", { name: /A\. Gutenberg/ }).click()
    await page.getByRole("button", { name: /A\. A printing guild/ }).click()
    // Scrolled into view from below (as keyboard focus or scrolling does), the button must not
    // end up hidden behind the sticky message bar.
    const check = page.getByRole("button", { name: "Check answers" })
    await check.evaluate(el => el.scrollIntoView({ block: "end" }))
    const covered = await check.evaluate(el => {
      const r = el.getBoundingClientRect()
      return !el.contains(document.elementFromPoint(r.x + r.width / 2, r.y + r.height / 2))
    })
    expect(covered, "the sticky message bar covers Check answers").toBe(false)
    await check.click()

    await expect(page.getByText("1 of 2 right")).toBeVisible()
    expect(api.quizAnswers).toEqual([{ turn: 1, step: 1, answers: { 1: "A", 2: "A" } }])
    await expect(page.getByText("To control what was read.")).toBeVisible()

    await page.getByRole("button", { name: "Library", exact: true }).click()
    await expect(page.getByText("1 of 4 sections studied")).toBeVisible()
    await expect(page.getByText("1/2 answers right")).toBeVisible()
    await expect(page.getByRole("button", { name: /Go back over Fear of Print/ })).toBeVisible()
  })

  test("an open quiz suggestion reopens that session without sending anything", async ({ page }) => {
    const api = await mockApi(page, { withOpenQuiz: true })
    await page.goto("/")
    await page.getByRole("button", { name: "Library", exact: true }).click()
    await page.getByRole("button", { name: /Finish your quiz/ }).click()

    await expect(page.locator("#turn-1")).toBeInViewport()
    await expect(page.getByText("Who built the first movable-type press in Europe?")).toBeVisible()
    await expect(page.getByText("section 3", { exact: true })).toBeVisible()
    expect(api.sentMessages).toEqual([])
  })
})

test("a document can be picked and studied with a typed message", async ({ page }) => {
  const api = await mockApi(page)
  await page.goto("/")
  await page.getByRole("button", { name: /print-culture\.pdf/ }).click()
  await page.getByPlaceholder(/Why did the Church fear print/).fill("Summarize it, then quiz me")
  await page.keyboard.press("Enter")

  await expect(page.getByText("Spread ideas", { exact: false })).toBeVisible()
  expect(api.sentMessages).toEqual(["Summarize it, then quiz me"])
  await page.getByRole("button", { name: /3 steps/ }).click()
  await expect(page.getByText("summarize -> quiz")).toBeVisible()
})
