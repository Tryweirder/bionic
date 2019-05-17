import pytest

import pandas as pd
import pandas.testing as pdt

from helpers import count_calls, df_from_csv_str

import bionic as bn


@pytest.fixture(scope='function')
def builder(tmp_path):
    builder = bn.FlowBuilder()
    builder.set('core__storage_cache__dir_name', str(tmp_path))
    return builder


PICKLABLE_VALUES = [
    1,
    'string',
    True,
    [1, 2, 3],
    {'a': 1, 'b': 2},
]


@pytest.mark.parametrize('value', PICKLABLE_VALUES)
def test_picklable_value(builder, value):
    @builder
    @bn.persist
    @bn.protocol.picklable()
    @count_calls
    def picklable_value():
        return value

    assert builder.build().get('picklable_value') == value
    assert builder.build().get('picklable_value') == value
    assert picklable_value.times_called() == 1


@pytest.mark.parametrize('value', PICKLABLE_VALUES)
def test_picklable_value_is_also_dillable(builder, value):
    @builder
    @bn.persist
    @bn.protocol.dillable()
    @count_calls
    def dillable_value():
        return value

    assert builder.build().get('dillable_value') == value
    assert builder.build().get('dillable_value') == value
    assert dillable_value.times_called() == 1


def test_dillable(builder):
    # Pickle doesn't support nested functions (at least in Python 2.7), but
    # dill should.
    def make_adder(x):
        def adder(y):
            return x + y
        return adder

    assert make_adder(3)(2) == 5

    @builder
    @bn.persist
    @bn.protocol.dillable()
    @count_calls
    def add_two():
        return make_adder(2)

    assert builder.build().get('add_two')(3) == 5
    assert builder.build().get('add_two')(3) == 5
    assert add_two.times_called() == 1


def test_simple_dataframe(builder):
    df_value = df_from_csv_str('''
    color,number
    red,1
    blue,2
    green,3
    ''')

    @builder
    @bn.persist
    @bn.protocol.frame()
    @count_calls
    def df():
        return df_value

    pdt.assert_frame_equal(builder.build().get('df'), df_value)
    pdt.assert_frame_equal(builder.build().get('df'), df_value)
    assert df.times_called() == 1


def test_typed_dataframe(builder):
    df_value = pd.DataFrame()
    df_value['int'] = [1, 2, 3]
    df_value['float'] = [1.0, 1.5, float('nan')]
    df_value['str'] = ['red', 'blue', None]
    df_value['time'] = pd.to_datetime([
        '2011-02-07',
        '2011-03-17',
        '2011-04-27',
    ])

    @builder
    @bn.persist
    @bn.protocol.frame()
    def df():
        return df_value

    pdt.assert_frame_equal(builder.build().get('df'), df_value)
    assert builder.build().get('df').dtypes.to_dict() ==\
        df_value.dtypes.to_dict()


def test_dataframe_index_cols(builder):
    @builder
    @bn.persist
    @bn.protocol.frame()
    def raw_df():
        return df_from_csv_str('''
        city,country,continent,metro_pop_mil
        Tokyo,Japan,Asia,38
        Delhi,India,Asia,26
        Shanghai,China,Asia,24
        Sao Paulo,Brazil,South America,21
        Mumbai,India,21
        Mexico City,Mexico,North America,21
        Beijing,China,Asia,20
        Osaka,Japan,Asia,20
        Cairo,Egypt,Africa,19
        New York,USA,North America,19
        ''')

    @builder
    @bn.persist
    @bn.protocol.frame()
    def counts_df(raw_df):
        return raw_df.groupby(['continent', 'country']).size()\
            .to_frame('count')

    df = builder.build().get('counts_df')
    assert df.loc['Asia'].loc['Japan']['count'] == 2
