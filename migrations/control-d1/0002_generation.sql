-- Which control database this is. A slot's stored policy revision and request
-- sequence mean something only against the database they came from: one that
-- is recreated starts both again, and one restored to an earlier time moves
-- both back. The value is random (1 to 2^53 - 1), served with every answer,
-- and a slot that sees one it does not hold drops its positions and starts
-- over. After a restore the operator gives the database a new one; see
-- docs/slot-control.md.
INSERT INTO control_meta (name, value) VALUES ('generation', abs(random() % 9007199254740991) + 1);
