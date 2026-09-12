import json

with open('companies_embed.json') as f:
    data_json = f.read()

with open('dashboard_template.html') as f:
    template = f.read()

out = template.replace('__COMPANY_DATA__', data_json)
with open('dashboard.html', 'w') as f:
    f.write(out)
print('wrote dashboard.html', len(out), 'bytes')
