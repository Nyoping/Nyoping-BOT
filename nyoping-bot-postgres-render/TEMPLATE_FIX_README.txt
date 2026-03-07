Render 500 fix:
- dashboard/templates/admin.html had invalid Jinja: {{ guild_id or  }}
- replaced with: {{ guild_id or '' }}
Apply: overwrite your repo file dashboard/templates/admin.html and redeploy.
