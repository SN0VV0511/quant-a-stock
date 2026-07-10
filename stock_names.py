import baostock as bs, json, re

# Read portfolio state to get stock codes
raw = open('data/portfolio_state.json').read()
codes = re.findall(r'\d{6}', raw)
codes = list(set(codes))

# Login to baostock
bs.login()

# Get stock names
prefix = {'5':'sh','0':'sz','3':'sz','6':'sh'}
result = {}
for c in codes:
    try:
        rs = bs.query_stock_basic(code=prefix.get(c[0],'sh')+'.'+c)
        while rs.next():
            result[c] = rs.get_row_data()[1]
    except Exception as e:
        print(f"Error getting info for {c}: {e}")

bs.logout()

# Print results
for c,n in sorted(result.items()):
    print(f"{c}:{n}")

# If no results, print the codes we found
if not result:
    print("No stock names found. Codes:", ", ".join(codes))