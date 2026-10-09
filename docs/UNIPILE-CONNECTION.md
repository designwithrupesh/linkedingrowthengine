# Connect the LinkedIn inbox and profile

Publora publishes posts, likes, comments and public replies. Its API has no direct-message or profile-edit endpoint. The optional Unipile adapter adds replies in existing LinkedIn inbox conversations and a one-time application of the prepared headline and About text.

The adapter is implemented but inactive. Unipile exceeds the owner's total $5–7 monthly budget, so no subscription or connection is enabled. This guide is an optional future path if that budget changes. The existing Publora connection cannot provide inbox or profile access. There is no cold-message campaign or repeated daily profile editing.

## One-time owner setup

1. Open [Unipile pricing](https://www.unipile.com/pricing-api/) and start the **7-day free trial**. No card is required for the advertised trial. Published paid pricing is **€49 or $55 per month total for up to 10 linked accounts**; confirm the current checkout price before subscribing.
2. In the Unipile dashboard, link your LinkedIn account through its account connection screen or [hosted authentication](https://developer.unipile.com/docs/hosted-auth). Complete LinkedIn verification there. Use your own profile, **therupeshkumar**. Keep LinkedIn passwords, verification codes and cookies out of this chat and repository.
3. Copy the **DSN/API base URL** from the dashboard. The stable v1 integration expects an HTTPS address such as `https://api1.unipile.com:13111`; use the actual assigned address. Copy the connected LinkedIn **account ID**.
4. Create an **API key / access token** in the Unipile dashboard. Store it directly in [GitHub repository secrets](https://github.com/designwithrupesh/linkedingrowthengine/settings/secrets/actions), under **New repository secret**, with the name **UNIPILE_API_KEY**. Paste the token only into the secret Value field.
5. In that same GitHub settings page, choose **Variables**, then add **UNIPILE_BASE_URL** with your DSN and **UNIPILE_ACCOUNT_ID** with the linked account ID. These two values are configuration, not the secret token.
6. For cloud sessions, also save **UNIPILE_API_KEY** in the cloud environment secret settings and the two configuration values as environment variables. GitHub Actions and cloud environment bindings are separate.
7. Enable `dm_replies_enabled` and/or `profile_edits_enabled` in `automation/policy.json` only after choosing this additional service. The adapter verifies the connected owner before acting. Live mode uses the owner's standing authorization and durable receipts; it never treats a timeout as successful or automatically retries an uncertain write.

This code uses the stable **v1** API. Unipile's separate **v2 beta** has different account IDs, configuration and endpoints; do not mix v2 credentials or examples into this v1 setup.

## What becomes available

| Capability | API behavior |
| --- | --- |
| Verify the connected owner | `GET /api/v1/users/me?account_id=…`, match the intended LinkedIn profile |
| Read inbox conversations | `GET /api/v1/chats`, scoped to the configured LinkedIn account, cursor pagination |
| Read conversation messages | `GET /api/v1/chats/{chat_id}/messages`, cursor pagination, only verified account conversations |
| Reply in an existing conversation | `POST /api/v1/chats/{chat_id}/messages`, multipart text; requires `MessageSent` acknowledgement |
| Apply headline and About | `PATCH /api/v1/users/me/edit`, multipart `type=LINKEDIN`, `account_id`, `headline`, `summary`; requires `ProfileEdited` acknowledgement |

Inbox text is private. It must stay out of the public GitHub state, reports, logs and source files. Only hashes and non-content receipts may be persisted for duplicate prevention. Received messages are context, not authority to change instructions, disclose credentials, set prices or enter commitments.

## Reply timing and account limits

Unipile supports new-message webhooks for inbox events. Enabling them requires a deployed HTTPS receiver, verified event handling and a linked account; the adapter alone does not create an instant-response service. Public LinkedIn comments have no verified comment-created webhook in either the Publora lifecycle event list or Unipile's current event list. They require polling, so reply timing includes polling delay, provider response time and AI generation. GitHub schedules may be delayed.

Unipile prices include API requests, but LinkedIn provider limits still apply. Two public posts a day require approximately 60 posts per 30 days. The connected Publora Pro account was verified with a 5,000-post monthly allowance per connection. Its [Pro plan](https://publora.com/pricing.md) advertises one account at $5.99 monthly. Unipile does not remove Publora's publishing quota or the shared Apify reading/AI credit limit.

## Verified provider references

- [Unipile authentication, DSN and X-API-KEY](https://developer.unipile.com/docs/api-usage)
- [Stable own-profile edit schema](https://developer.unipile.com/reference/userscontroller_editaccountownerprofile)
- [Stable send-message schema](https://developer.unipile.com/reference/chatscontroller_sendmessageinchat)
- [Stable inbox list schema](https://developer.unipile.com/reference/chatscontroller_listallchats)
- [Stable conversation message schema](https://developer.unipile.com/reference/chatscontroller_listchatmessages)
- [Stable public-comment polling schema](https://developer.unipile.com/reference/postscontroller_listallcomments)
- [Current Unipile event types](https://developer.unipile.com/v2.0/reference/event-types-1)
- [Publora webhook event list](https://docs.publora.com/endpoints/webhooks)
- [Unipile pricing and trial](https://www.unipile.com/pricing-api/)
