import json

# Read portfolio state
with open('data/portfolio_state.json', 'r') as f:
    portfolio = json.load(f)

# Extract stock info from portfolio state
positions = portfolio.get('positions', {})
codes_info = {}
for code, info in positions.items():
    codes_info[code] = {
        'name': info['name'],
        'shares': info['shares'],
        'avg_cost': info['avg_cost'],
        'current_price': info['current_price']
    }

# Print results
print("Current positions:")
for code, info in codes_info.items():
    profit_pct = ((info['current_price'] - info['avg_cost']) / info['avg_cost']) * 100
    print(f"{code}: {info['name']} - {info['shares']}股, 成本{info['avg_cost']:.3f}, 现价{info['current_price']:.3f}, 盈亏{profit_pct:+.2f}%")