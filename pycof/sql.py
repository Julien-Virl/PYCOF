import os
import sys
import getpass
import warnings

import re

import pandas as pd
import numpy as np

from tqdm import tqdm
import datetime

from .sqlhelper import _get_config, _get_credentials, SSHTunnel
from .sqlhelper import _insert_data, _cache
from .data import write, f_read
from .format import file_age, verbose_display


#######################################################################################################################

# TODO: Test send_email by list of recipients
# TODO: remote_execute_sql with dump to S3 or to S3 and Redshift.

#######################################################################################################################

# Publish or read from DB
def remote_execute_sql(sql_query="", query_type="", table="", data={}, credentials={}, verbose=True, connection='direct', autofill_nan=True,
                       engine='default', cache=False, cache_name=None, *args, **kwargs):
    """Simplified function for executing SQL queries. Will look at the credentials at :obj:`/etc/.pycof/config.json`. User can also pass a dictionnary for
    credentials.

    :Parameters:
        * **sql_query** (:obj:`str`): SQL query to be executed. Allows a string containing the SQL or a path containing the extension '.sql' (defaults "").
        * **query_type** (:obj:`str`): Type of SQL query to execute. Can either be SELECT, INSERT, COPY, DELETE or UNLOAD (defaults "SELECT").
        * **table** (:obj:`str`): Table in which we want to operate, only used for INSERT and DELETE (defaults "").
        * **data** (:obj:`pandas.DataFrame`): Data to load on the database (defaults {}).
        * **credentials** (:obj:`dict`): Credentials to use to connect to the database. Check the FAQ for arguments required depending on your type of connection. You can also provide the credentials path or the json file name from '/etc/.pycof/' (defaults {}).
        * **verbose** (:obj:`bool`): Display progression bar (defaults True).
        * **connection** (:obj:`str`): Type of connection to establish. Can either be 'direct', 'IAM' or 'SSH' (defaults 'direct').
        * **autofill_nan** (:obj:`bool`): Replace NaN values by 'NULL' (defaults True).
        * **cache** (:obj:`str`): Caches the data to avoid running again the same SQL query (defaults False). Provide a :obj:`str` for the cache time.
        * **cache_name** (:obj:`str`): File name for storing cache data, if None the name will be generated by hashing the SQL (defaults None).
        * **\\*\\*kwargs** (:obj:`str`): Arguments to be passed to the :py:meth:`pycof.data.f_read` function.

    .. warning:: Since version 1.2.0, argument :obj:`useIAM` is replaced by :obj:`connection`.
        To connect via AWS IAM, use :obj:`connection='IAM'`.
        You can also establish an SSH tunnel with :obj:`connection='SSH'`, check FAQ below for credentials required.

    .. warning:: Since version 1.2.0, default file for credentials on Unix needs to be :obj:`/etc/.pycof/config.json`.
        You can still use your old folder location by providing the full path.
        Note that you can also provide the crendentials' file name in the folder :obj:`/etc/.pycof/` without having to specify the extension.
        Check FAQ below for more details.

    :Configuration: The function requires the below arguments in the configuration file.

        * :obj:`DB_USER`: Database user.
        * :obj:`DB_PASSWORD`: Password for connection to database, can remain empty if :obj:`connection='IAM'`.
        * :obj:`DB_HOST`: End point (hostname) of the database.
        * :obj:`DB_PORT`: Port to access the database.
        * :obj:`CLUSTER_NAME`: Name of the Redshift cluster. Can be accessible on the Redshift dashboard. Only required if cluster to access is Redshift.
        * :obj:`SSH_USER`: User on the server to use for SSH connection. Only required if :obj:`connection='SSH'`.
        * :obj:`SSH_KEY`: Path to SSH private key for SSH connection. Only required if :obj:`connection='SSH'` and path to the key is not default (usually :obj:`/home/<username>/.ssh/id_rsa` on Linux/MacOS or :obj:`'C://Users/<username>/.ssh/id_rsa` on Windows).
        * :obj:`SSH_PASSWORD`: Password of the SSH user if no key is provided (or key is not registered on the destination host).

        .. code-block:: python

            {
            "DB_USER": "",
            "DB_PASSWORD": "",
            "DB_HOST": "",
            "DB_PORT": "3306",
            "__COMMENT_1__": "Redshift specific",
            "CLUSTER_NAME": "",
            "__COMMENT_2__": "SSH specific",
            "SSH_USER": "",
            "SSH_KEY": "",
            "SSH_PASSWORD": ""
            }

    :Example:
        >>> df = pycof.remote_execute_sql("SELECT * FROM SCHEMA.TABLE LIMIT 10")

    :Returns:
        * :obj:`pandas.DataFrame`: Result of an SQL query if :obj:`query_type = "SELECT"`.

        Metadata are also available to users with addtionnal information regarding the SQL query and the file.

        * :obj:`df.meta.cache.creation_date`: Datetime when the query has been run and cached.
        * :obj:`df.meta.cache.cache_path`: Path to the local cached file.
        * :obj:`df.meta.cache.query_path`: Path to the local cached SQL query.
        * :obj:`df.meta.cache.age()`: Function to evaluate the age of the data file. See :py:meth:`pycof.misc.file_age` for formats available.
    """

    # ============================================================================================
    # Define the SQL type
    all_query_types = ['SELECT', 'INSERT', 'DELETE', 'COPY', 'UNLOAD', 'UPDATE', 'CREATE', 'GRANT']

    if (query_type != ""):
        # Use user input if query_type is not as its default value
        sql_type = query_type
    elif type(data) == pd.DataFrame:
        # If data is provided, use INSERT sql_type
        sql_type = 'INSERT'
    elif type(sql_query) == pd.DataFrame:
        # If data is instead of an SQL query, use INSERT sql_type
        sql_type = 'INSERT'
        data = sql_query
    elif ("UNLOAD " in sql_query.upper()):
        sql_type = 'UNLOAD'
    elif ("COPY " in sql_query.upper()):
        sql_type = 'COPY'
    elif ("UPDATE " in sql_query.upper()):
        sql_type = 'UPDATE'
    elif (sql_query != ""):
        # If a query is inserted, use select.
        # For DELETE or COPY, user needs to provide the query_type
        sql_type = "SELECT"
    else:
        allowed_queries = f"Your query_type value is not correct, allowed values are {', '.join(all_query_types)}"
        # Check if the query_type value is correct
        raise ValueError(allowed_queries + f'. Got {query_type}')
        # assert query_type.upper() in all_query_types, allowed_queries

    # ============================================================================================
    # Process SQL query
    if sql_type != 'INSERT':
        if (sql_query != "") & ('.sql' in sql_query.lower()):
            # Can read an external file is path is given as sql_query
            sql_query = f_read(sql_query, extension='sql', **kwargs)
            assert sql_query != '', 'Could not read your SQL file properly. Please make sure your file is saved or check your path.'

    # ============================================================================================
    # Credentials load
    config = _get_credentials(_get_config(credentials), connection)

    # ============================================================================================
    # Start the connection
    with SSHTunnel(config=config, connection=connection, engine=engine) as tunnel:
        # ============================================================================================
        # Database connector

        # ============================================================================================
        # Set default value for table
        if (sql_type == 'SELECT'):  # SELECT
            if (table == ""):  # If the table is not specified, we get it from the SQL query
                table = sql_query.upper().replace('\n', ' ').split('FROM ')[1].split(' ')[0]
            elif (sql_type == 'SELECT') & (table.upper() in sql_query.upper()):
                table = table
            else:
                raise SyntaxError('Argument table does not match with SQL statement')

        # ========================================================================================
        # SELECT - Read query
        if sql_type.upper() == "SELECT":
            if cache:
                read = _cache(sql_query, tunnel, sql_type, cache_time=cache, verbose=verbose, cache_file_name=cache_name)
            else:
                conn = tunnel.connector()
                read = pd.read_sql(sql_query, conn, coerce_float=False)
                # Close SQL connection
                conn.close()
            return(read)
        # ============================================================================================
        # INSERT - Load data to the db
        elif sql_type.upper() == "INSERT":
            conn = tunnel.connector()
            _insert_data(data=data, table=table, connector=conn, autofill_nan=autofill_nan, verbose=verbose)

        # ============================================================================================
        # DELETE / COPY / UNLOAD - Execute SQL command which does not return output
        elif sql_type.upper() in ["CREATE", "GRANT", "DELETE", "COPY", "UNLOAD", "UPDATE"]:
            if table.upper() in sql_query.upper():
                conn = tunnel.connector()
                cur = conn.cursor()
                cur.execute(sql_query)
                conn.commit()
            else:
                raise ValueError('Table does not match with SQL query')
        else:
            raise ValueError(f'Unknown query_type, should be as: {all_query_types}')

        # Close SQL connection
        conn.close()

#######################################################################################################################
