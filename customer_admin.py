"""Local reviewed configuration CLI. No remote API and no customer-data export."""
import argparse,json
from pathlib import Path
from customer_memory import CustomerMemory
from customer_orders import apply_config

OPERATIONS={
 'profile':('confirm_profile',('customer_id','name','sender','confirmed_by')),
 'binding':('bind',('scope','external_id','customer_id','confirmed_by')),
 'variant':('confirm_variant',('variant_id','customer_id','sender','cue','confirmed_by')),
 'allow_room':('allow_room',('room','confirmed_by')),
 'disable_room':('disable_room',('room','confirmed_by')),
 'unbind':('unbind',('scope','external_id','customer_id','confirmed_by')),
 'remove_variant':('remove_variant',('variant_id','customer_id','confirmed_by')),
 'remove_profile':('remove_profile',('customer_id','confirmed_by'))}

def apply_changes(memory,changes):
    if not isinstance(changes,list):raise ValueError('Reviewed changes must be a list')
    with memory.atomic():
        for change in changes:
            if change.get('operation') not in OPERATIONS:raise ValueError('Unknown configuration operation')
            method,keys=OPERATIONS[change['operation']]
            if set(change)!={'operation',*keys}:raise ValueError('Unexpected or missing configuration fields')
            getattr(memory,method)(**{key:change[key] for key in keys})

def main():
    parser=argparse.ArgumentParser();parser.add_argument('--db',required=True)
    group=parser.add_mutually_exclusive_group(required=True);group.add_argument('--config');group.add_argument('--changes')
    args=parser.parse_args();memory=CustomerMemory(args.db)
    try:
        payload=json.loads(Path(args.config or args.changes).read_text())
        if args.config:apply_config(memory,payload)
        else:apply_changes(memory,payload)
        print('Reviewed customer configuration applied atomically. No orders sent or written.')
    finally:memory.db.close()
if __name__=='__main__':main()
