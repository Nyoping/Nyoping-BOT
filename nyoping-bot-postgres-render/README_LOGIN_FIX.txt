Login Fix Patch (v4)

Fixes:
- Login form was incorrectly generating a GET request to a quoted path like /%22/admin-login/%22?... resulting in 404.
- Restores correct POST /admin-login and masks password input.

Apply:
- Replace <Root Directory>/dashboard/templates/index.html with this file
- Commit to GitHub and deploy on Render
