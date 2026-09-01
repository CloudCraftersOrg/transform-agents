from tools.finops import monthly_run_rate


def test_run_rate_sums_instances_and_ebs():
    r = monthly_run_rate(
        [
            {"instance_type": "t3.medium", "ebs_gib": 30},
            {"instance_type": "t3.small", "ebs_gib": 20},
        ]
    )
    assert r == round(29.95 + 30 * 0.08 + 14.98 + 20 * 0.08, 2)


def test_unknown_instance_type_is_zero_compute():
    assert monthly_run_rate([{"instance_type": "x9.mega", "ebs_gib": 0}]) == 0.0
