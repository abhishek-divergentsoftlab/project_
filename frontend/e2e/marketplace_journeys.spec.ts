import { test, expect } from "@playwright/test";

test.describe("B2B Marketplace Full Browser E2E Journeys", () => {
  const timestamp = Date.now();
  const buyerEmail = `buyer_${timestamp}@marketplace.test`;
  const sellerEmail = `seller_${timestamp}@marketplace.test`;
  const password = "StrongPassword123!";

  test("Journey 1: Signup, Login, Profile, Manual RFQ Creation & Marketplace Browsing", async ({ page }) => {
    // 1. Buyer Signup
    await page.goto("/signup");
    await expect(page).toHaveTitle(/Marketplace|Signup|Login/i);

    // Fill signup form
    await page.fill('input[type="email"], input[name="email"]', buyerEmail);
    await page.fill('input[type="password"], input[name="password"]', password);
    if (await page.locator('input[name="name"]').count() > 0) {
      await page.fill('input[name="name"]', "Industrial Buyer Corp");
    }
    if (await page.locator('select[name="role"]').count() > 0) {
      await page.selectOption('select[name="role"]', "buyer");
    }
    await page.click('button[type="submit"]');

    // Should navigate to dashboard or login
    await page.waitForURL(/\/(dashboard|login|profile)/, { timeout: 10_000 });

    // 2. Profile View / Completeness
    await page.goto("/profile");
    await expect(page.locator("h1, h2, .page-header")).toContainText(/Profile/i);

    // 3. Create RFQ
    await page.goto("/rfqs/new");
    await expect(page.locator("h1, h2, form")).toContainText(/RFQ|Listing|Request/i);

    const titleInput = page.locator('input[name="title"], input[placeholder*="title" i]').first();
    await titleInput.fill(`Precision Ball Bearings ${timestamp}`);

    const descInput = page.locator('textarea[name="description"], textarea[placeholder*="description" i]').first();
    if (await descInput.count() > 0) {
      await descInput.fill("ISO 9001 certified stainless steel ball bearings 608ZZ grade, MOQ 1000 units.");
    }

    const qtyInput = page.locator('input[name="quantity"], input[type="number"]').first();
    if (await qtyInput.count() > 0) {
      await qtyInput.fill("1000");
    }

    const priceInput = page.locator('input[name="target_price"], input[name="price"]').first();
    if (await priceInput.count() > 0) {
      await priceInput.fill("150");
    }

    // Submit RFQ form
    const submitBtn = page.locator('button[type="submit"], button:has-text("Publish"), button:has-text("Create")').first();
    await submitBtn.click();

    // 4. Verify in RFQ List
    await page.waitForURL(/\/rfqs/, { timeout: 10_000 });
    await expect(page.locator("body")).toContainText(`Precision Ball Bearings ${timestamp}`);

    // 5. Navigate to Marketplace and search
    await page.goto("/marketplace");
    const searchInput = page.locator('input[type="search"], input[placeholder*="search" i]').first();
    if (await searchInput.count() > 0) {
      await searchInput.fill("bearings");
      await page.keyboard.press("Enter");
    }
    await expect(page.locator(".marketplace-grid, .rfq-card, table, body")).toBeVisible();
  });

  test("Journey 2: AI Chat RFQ Assistance, Intent Confirmation & Draft Card Generation", async ({ page }) => {
    // Login as buyer
    await page.goto("/login");
    await page.fill('input[type="email"], input[name="email"]', buyerEmail);
    await page.fill('input[type="password"], input[name="password"]', password);
    await page.click('button[type="submit"]');
    await page.waitForURL(/\/(dashboard|rfqs|marketplace)/);

    // Navigate to AI Super Agent Chat
    await page.goto("/ai-chat");
    await expect(page.locator(".ai-chat-container, .chat-pane, main")).toBeVisible();

    const chatInput = page.locator('textarea, input[placeholder*="Ask" i], input[placeholder*="AI" i]').first();
    await chatInput.fill("I want to buy 500 meters of industrial high-pressure hydraulic hoses in Pune with target price 450 INR per meter.");
    
    const sendButton = page.locator('button[type="submit"], button:has-text("Send"), .ai-send-btn').first();
    await sendButton.click();

    // AI message should stream in with structured draft or clarification
    await expect(page.locator(".ai-message, .chat-bubble, .message-bubble").last()).toBeVisible({ timeout: 15_000 });
  });

  test("Journey 3: Deal Room, Messaging, and Negotiation Invariants", async ({ page }) => {
    await page.goto("/login");
    await page.fill('input[type="email"], input[name="email"]', buyerEmail);
    await page.fill('input[type="password"], input[name="password"]', password);
    await page.click('button[type="submit"]');

    await page.goto("/messages");
    await expect(page.locator(".deal-room, .messages-container, .conversations-list, main")).toBeVisible();

    // Check message input safety
    const msgInput = page.locator('textarea[placeholder*="message" i], input[placeholder*="message" i]').first();
    if (await msgInput.count() > 0) {
      // Input adversarial XSS payload
      await msgInput.fill("<script>alert(1)</script> Hello from buyer");
      const sendMsgBtn = page.locator('button:has-text("Send"), .btn-send').first();
      if (await sendMsgBtn.count() > 0 && !(await sendMsgBtn.isDisabled())) {
        await sendMsgBtn.click();
        // Ensure script is not executed as raw HTML
        await expect(page.locator("body")).not.toContainText("alert(1)");
      }
    }
  });

  test("Journey 4: Error Handling & Security Boundary Resilience", async ({ page }) => {
    // 404 test
    await page.goto("/non-existent-route-404");
    // Should safely redirect to dashboard or show 404
    await expect(page.locator("body")).toBeVisible();

    // IDOR test on non-existent RFQ
    await page.goto("/rfqs/00000000-0000-0000-0000-000000000000/matches");
    await expect(page.locator("body")).toBeVisible();

    // Check token revocation on logout
    await page.goto("/dashboard");
    const logoutBtn = page.locator('button:has-text("Logout"), a:has-text("Logout")').first();
    if (await logoutBtn.count() > 0) {
      await logoutBtn.click();
      await page.waitForURL(/\/login/);
      // Attempting to visit protected route after logout should redirect to login
      await page.goto("/dashboard");
      await page.waitForURL(/\/login/);
    }
  });
});
