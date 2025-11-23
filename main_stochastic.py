from typing import List

from ortools.linear_solver import pywraplp
from problem_stochastic import RPP


def nested_shape(lst):
    shape = []
    while isinstance(lst, list):
        shape.append(len(lst))
        if not lst:  # empty list — stop here
            break
        lst = lst[0]
    return tuple(shape)

class Variables:
    def __init__(self, 
                 solver: pywraplp.Solver,
                 problem: RPP):
        self.capitals = [
            [solver.NumVar(-solver.infinity(), solver.infinity(), f"F^{scenario}_{period}") for period in range(problem.num_periods+1)]
            for scenario in range(problem.num_scenarios)
        ]
        # num_testers[m]
        self.num_testers = [None]+[solver.IntVar(problem.initial_num_testers[(m,)], solver.infinity(), f"K_({m})") for m in range(1, problem.num_testers+1)]
        # num_handlers[h][a]
        self.num_handlers = [[None]] + [
                [None] + [solver.IntVar(problem.initial_num_handlers[(h, a)], solver.infinity(), f"K^{h}_{a}") for a in range(1, problem.num_handlers+1)]
                for h in range(1, problem.num_handler_categories+1)
            ]
        # num_acquired_testers[p][m][z]
        self.num_acquired_testers = [
            [
                [solver.IntVar(0, solver.infinity(), f"X_({p},{m},{z})") for z in range(problem.num_tester_channels+1)]
                for m in range(problem.num_testers+1)
            ]
            for p in range(problem.num_periods+1)
        ]
        # num_acquired_handlers[p][h][a][z]
        self.num_acquired_handlers = [
            [
                [
                    [solver.IntVar(0, solver.infinity(), f"X^{h}_({p},{a},{z})") for z in range(problem.num_handler_channels+1)]
                    for a in range(problem.num_handlers+1)
                ]
                for h in range(problem.num_handler_categories+1)
            ]
            for p in range(problem.num_periods+1)
        ]
        # num_produced_main[p][m][t]
        self.num_produced_main = [
            [
                [solver.NumVar(0, solver.infinity(), f"Q_({p},{m},{t})") for t in range(problem.num_products+1)]
                for m in range(problem.num_testers+1)
            ]
            for p in range(problem.num_periods+1)
        ]
        # num_produced_combined[p][m][h][a][t]
        self.num_produced_by_handler_categories = [
            [
                [
                    [
                        [solver.NumVar(0, solver.infinity(), f"Q^{h}_({p},{m},{a},{t})") for t in range(problem.num_products+1)]
                        for a in range(problem.num_handlers+1)
                    ]
                    for h in range(problem.num_handler_categories+1)
                ]
                for m in range(problem.num_testers+1)
            ]
            for p in range(problem.num_periods+1)
        ]
        #product_capacity_loading_qtys[scenario][p][t]
        self.product_capacity_loading_qtys = [
            [
                [solver.NumVar(-solver.infinity(), solver.infinity(), f"S^{scenario}_({p},{t})") for t in range(problem.num_products+1)]
                for p in range(problem.num_periods+1)
            ]
            for scenario in range(problem.num_scenarios)
        ]
        self.Spos = [
            [
                [solver.NumVar(0, solver.infinity(), f"Spos^{scenario}_({p},{t})") for t in range(problem.num_products+1)]
                for p in range(problem.num_periods+1)
            ]
            for scenario in range(problem.num_scenarios)
        ]
        self.Sneg = [
            [
                [solver.NumVar(0, solver.infinity(), f"Sneg^{scenario}_({p},{t})") for t in range(problem.num_products+1)]
                for p in range(problem.num_periods+1)
            ]
            for scenario in range(problem.num_scenarios)
        ]
        #product_capacity_loading_costs[scenario][p][t]
        self.product_capacity_loading_costs = [
            [
                [solver.NumVar(-solver.infinity(), solver.infinity(), f"V^{scenario}_({p},{t})") for t in range(problem.num_products+1)]
                for p in range(problem.num_periods+1)
            ]
            for scenario in range(problem.num_scenarios)
        ]
        self.y = [
            [
                [solver.BoolVar(f"y^{scenario}_({p},{t})") for t in range(problem.num_products+1)]
                for p in range(problem.num_periods+1)
            ]
            for scenario in range(problem.num_scenarios)
        ]
        # Profit per scenario (π^ξ in paper's notation, scenario profit)
        self.profits = [solver.NumVar(-solver.infinity(), solver.infinity(), f"profit^{scenario}") for scenario in range(problem.num_scenarios)]

        # Mean absolute deviation (MAD) auxiliary variables
        self.mean_profit = solver.NumVar(-solver.infinity(), solver.infinity(), "mean_profit")
        self.profit_deviations = [solver.NumVar(0, solver.infinity(), f"deviation^{scenario}") for scenario in range(problem.num_scenarios)]

        self.BigM = 1e8  # Reduced for numerical stability
        
        


def solve(problem: RPP, lambda_param: float = 0.5, time_limit_minutes: float = 5.0):
    """
    Solve the stochastic resource portfolio planning problem.
    
    Args:
        problem: RPP problem instance
        lambda_param: Risk aversion parameter (0-1). 0=pure profit maximization, 1=pure risk minimization
        time_limit_minutes: Maximum solving time in minutes (default: 5.0)
    """
    solver: pywraplp.Solver = pywraplp.Solver.CreateSolver("SCIP")
    vars = Variables(solver, problem)
    # Constraint (2)
    for p in problem.periods:
        for m in problem.testers:
            # 1️⃣ Available testers in period p, tester type m
            num_available_testers = (
                vars.num_testers[m]
                + sum(vars.num_acquired_testers[p][m][z] for z in problem.tester_channels)
            )

            # 2️⃣ Effective utilization rate (hours × utilization fraction)
            total_utilization_rate = (
                problem.tester_work_hours[p, m]
                * problem.tester_target_utils[p, m]
            )

            # 3️⃣ Production workload adjusted by tester ability
            num_produced_main = sum(
                (problem.tester_ablities[m, t] * vars.num_produced_main[p][m][t])/(problem.tester_throughputs[m,t]*total_utilization_rate)
                for t in problem.products
            )

            # 4️⃣ Capacity constraint
            solver.Add(
                num_available_testers >= num_produced_main,
                f"TesterCapacity[p={p},m={m}]"
            )
    
    # Constraint (3)
    for p in problem.periods:
        for m in problem.testers:
            for h in problem.handler_categories:
                for t in problem.products:
                    sum_produced_by_categories = sum(
                        problem.handler_ablities[m,h,a,t]*vars.num_produced_by_handler_categories[p][m][h][a][t]
                        for a in problem.handlers
                    )
                    solver.Add(sum_produced_by_categories == vars.num_produced_main[p][m][t])
    
    # Constraint (4)
    for p in problem.periods:
        for a in problem.handlers:
            for h in problem.handler_categories:
                num_available_handlers = vars.num_handlers[h][a] + sum(vars.num_acquired_handlers[p][h][a][z] for z in problem.handler_channels)
                total_utilization_rate = problem.handler_work_hours[p,h,a]*problem.handler_target_utils[p,h,a]
                sum_produced_by_categories = sum(
                        (problem.handler_ablities[m,h,a,t]*vars.num_produced_by_handler_categories[p][m][h][a][t])/(problem.handler_throughputs[m,h,a,t]*total_utilization_rate)
                        for m in problem.testers for t in problem.products
                    )
                solver.Add(num_available_handlers >= sum_produced_by_categories)
    
    # Constraint (5prelude) - scenario-indexed
    for scenario in range(problem.num_scenarios):
        for p in range(problem.num_periods+1):
            for t in problem.products:
                solver.Add(vars.Spos[scenario][p][t] <= vars.BigM * vars.y[scenario][p][t])
                solver.Add(vars.Sneg[scenario][p][t] <= vars.BigM * (1 - vars.y[scenario][p][t]))
                solver.Add(vars.product_capacity_loading_qtys[scenario][p][t] == vars.Spos[scenario][p][t] - vars.Sneg[scenario][p][t])
        for t in problem.products:
            solver.Add(vars.product_capacity_loading_qtys[scenario][0][t] == problem.initial_capacity_loading_qty[(t,)])

    # Constraint (5) - scenario-indexed with stochastic demands
    scenarios = range(1, problem.num_scenarios + 1)  # scenarios are 1-indexed in problem.demands_mts
    for s_idx, s in enumerate(scenarios):
        for p in problem.periods:
            for t in problem.products:
                num_produced_main = sum(
                    (problem.tester_ablities[m, t] * vars.num_produced_main[p][m][t])
                    for m in problem.testers
                )
                solver.Add(vars.product_capacity_loading_qtys[s_idx][p][t] == vars.product_capacity_loading_qtys[s_idx][p-1][t] + num_produced_main - problem.demands_mts[s,p,t]) 
    
    # Constraint (6) - using expected MTO demand across scenarios
    for p in problem.periods:
        for t in problem.products:
            num_produced_main = sum(
                (problem.tester_ablities[m, t] * vars.num_produced_main[p][m][t])
                for m in problem.testers
            )
            # Use expected demand across scenarios for first-stage production decision
            expected_mto_demand = sum(problem.demands_mto[s,p,t] for s in scenarios) / problem.num_scenarios
            solver.Add(num_produced_main <= expected_mto_demand)

    # Constraint (7) - scenario-indexed
    for scenario in range(problem.num_scenarios):
        for p in problem.periods:
            for t in problem.products:
                excess_cost = problem.excess_production_cost[p,t]*vars.Spos[scenario][p][t]
                shortage_cost = problem.shortage_cost[p,t]*vars.Sneg[scenario][p][t]
                solver.Add(vars.product_capacity_loading_costs[scenario][p][t] == excess_cost + shortage_cost)

    # Constraint (8prelude) - set initial capital for all scenarios
    for scenario in range(problem.num_scenarios):
        solver.Add(vars.capitals[scenario][0] == problem.capital)

    # Constraint (8) - scenario-indexed capital balance
    for s_idx, s in enumerate(scenarios):
        for p in problem.periods:
            tester_borrow_total_cost = sum(problem.tester_borrow_prices[p,m,z]*vars.num_acquired_testers[p][m][z] for m in problem.testers for z in problem.tester_channels)
            handler_borrow_total_cost = sum(problem.handler_borrow_prices[p,h,a,z]*vars.num_acquired_handlers[p][h][a][z] for z in problem.handler_channels for a in problem.handlers for h in problem.handler_categories)
            inventory_cost = sum(vars.product_capacity_loading_costs[s_idx][p][t] for t in problem.products)
            total_profit_mts = sum(problem.product_profits[p,t]*problem.demands_mts[s,p,t] for t in problem.products)
            total_profit_mto = sum(problem.product_profits[p,t]*vars.num_produced_main[p][m][t] for t in problem.products for m in problem.testers)
            last_capital = vars.capitals[s_idx][p-1]*(1+problem.interest_rates[p])
            solver.Add(vars.capitals[s_idx][p] == last_capital - tester_borrow_total_cost - handler_borrow_total_cost - inventory_cost + total_profit_mts + total_profit_mto)

    # Profit calculation per scenario (Equation 9 from paper)
    last_period = max(problem.periods)
    compound_interest = 1
    for p in problem.periods:
        compound_interest *= (1 + problem.interest_rates[p])

    # First-stage costs (same across all scenarios)
    tester_purchase_cost = sum((problem.tester_initial_prices[(m,)]- problem.tester_salvage_prices[(m,)])*(vars.num_testers[m]-problem.initial_num_testers[(m,)]) for m in problem.testers)
    handler_purchase_cost = sum((problem.handler_initial_prices[h,a]-problem.handler_salvage_prices[h,a])*(vars.num_handlers[h][a]-problem.initial_num_handlers[h,a]) for h in problem.handler_categories for a in problem.handlers)

    # Calculate profit for each scenario
    for s_idx in range(problem.num_scenarios):
        last_capital_scenario = vars.capitals[s_idx][last_period]/compound_interest
        solver.Add(vars.profits[s_idx] == last_capital_scenario - tester_purchase_cost - handler_purchase_cost)

    # Multi-scenario objective with MAD risk measure (Equation 1 from paper)
    # Calculate mean profit
    solver.Add(vars.mean_profit == sum(vars.profits) / problem.num_scenarios)

    # Calculate absolute deviations using auxiliary variables
    # For each scenario: |profit[s] - mean_profit| = deviation[s]
    for s_idx in range(problem.num_scenarios):
        # deviation[s] >= profit[s] - mean_profit
        solver.Add(vars.profit_deviations[s_idx] >= vars.profits[s_idx] - vars.mean_profit)
        # deviation[s] >= -(profit[s] - mean_profit) = mean_profit - profit[s]
        solver.Add(vars.profit_deviations[s_idx] >= vars.mean_profit - vars.profits[s_idx])

    # Calculate MAD (mean absolute deviation)
    mad = sum(vars.profit_deviations) / problem.num_scenarios

    # Expected profit (same as mean_profit, but calculated explicitly for clarity)
    expected_profit = sum(vars.profits) / problem.num_scenarios

    # Objective: (1-λ) * expected_profit - λ * MAD
    # λ = 0: pure profit maximization
    # λ = 1: pure risk minimization
    objective = (1 - lambda_param) * expected_profit - lambda_param * mad

    solver.Maximize(objective)
    solver.SetNumThreads(1)  # Single-threaded to avoid numerical issues
    
    # Set time limit (convert minutes to milliseconds)
    time_limit_ms = int(time_limit_minutes * 60 * 1000)
    solver.SetTimeLimit(time_limit_ms)
    
    # Enable verbose logging from OR-Tools solver
    solver.EnableOutput()
    
    # Set SCIP-specific parameters
    # display/verblevel: verbosity level (0=off, 1=errors, 2=warnings, 3=info, 4=verbose, 5=full)
    # display/freq: frequency for displaying node information (higher = less frequent)
    solver.SetSolverSpecificParametersAsString("display/verblevel = 3\ndisplay/freq = 10000")
    
    print("="*80)
    print(f"Starting optimization with {len(solver.variables())} variables and {len(solver.constraints())} constraints")
    print(f"Problem: {problem.num_scenarios} scenarios, {problem.num_periods} periods, {problem.num_products} products")
    print(f"Time limit: {time_limit_ms / 1000:.0f} seconds ({time_limit_ms / 60000:.0f} minutes)")
    print("="*80)
    
    status = solver.Solve()
    
    print("="*80)
    print("SOLVER FINISHED!")
    print("="*80)

    if status == pywraplp.Solver.OPTIMAL:
        print("✓ Status: OPTIMAL solution found")
        print(f"✓ Objective Value: {solver.Objective().Value():,.2f}")
    elif status == pywraplp.Solver.FEASIBLE:
        print("⚠ Status: FEASIBLE solution found (not proven optimal)")
        print(f"⚠ This may be due to timeout or early termination")
        print(f"✓ Objective Value: {solver.Objective().Value():,.2f}")
    elif status == pywraplp.Solver.INFEASIBLE:
        print("✗ Status: Problem is INFEASIBLE - no solution exists")
    elif status == pywraplp.Solver.UNBOUNDED:
        print("✗ Status: Problem is UNBOUNDED")
    else:
        print(f"? Status: {status}")
    
    wall_time_sec = solver.WallTime() / 1000.0
    print(f"Wall time: {wall_time_sec:.2f} seconds ({wall_time_sec/60:.2f} minutes)")
    
    if wall_time_sec >= (time_limit_ms / 1000.0 * 0.95):  # Within 95% of time limit
        print("⏱ Note: Solver reached or approached the time limit")
    print("="*80)
    
    if status == pywraplp.Solver.OPTIMAL or status == pywraplp.Solver.FEASIBLE:
        print("Objective =", solver.Objective().Value())
        print(f"Status code: {status}")

        # Safely extract solution - only print non-zero values to avoid segfault
        print("\nKey decision variables (non-zero values):")
        try:
            for var in solver.variables():
                val = var.solution_value()
                if abs(val) > 1e-6:  # Only print significant values
                    print(f"{var.name():<30s} = {val:,.6f}")
        except Exception as e:
            print(f"Warning: Could not extract all solution values: {e}")
    else:
        print(f"Status code: {status}")

def run():
    # Create problem with 1 scenario to test if basic model works
    # Starting simple - if this works, we'll try 2, then more
    print("Creating problem instance...")
    problem = RPP(num_scenarios=1, distribution="uniform", variance=0.1)
    print(f"Problem created: {problem.num_scenarios} scenarios, {problem.num_periods} periods")

    # Solve with balanced risk-return tradeoff (λ=0.5)
    # Note: With 1 scenario, MAD = 0, so this is essentially deterministic
    print("Starting solver...")
    solve(problem, lambda_param=0.5, time_limit_minutes=2.0)


if __name__ == "__main__":
    run()
